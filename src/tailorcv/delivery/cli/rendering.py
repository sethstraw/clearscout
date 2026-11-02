"""Render command results for the terminal."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tailorcv.delivery.cli.support import terminal
from tailorcv.domain.requirements import gate_kind_label
from tailorcv.shared import results
from tailorcv.shared.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tailorcv.application.tailoring.batch import Batch
    from tailorcv.application.tailoring.elicit import Outcome
    from tailorcv.application.tailoring.fit import Reading
    from tailorcv.application.tailoring.pipeline import MaterializedRun, RunResult, ScreenResult
    from tailorcv.application.tailoring.stages.jobspec import JobSpec
    from tailorcv.application.tailoring.stages.match import EvidenceMap, GateAnswer, Match


def _classification_counts(evidence: EvidenceMap) -> dict[str, int]:
    """Count evidence classifications for the screening tally."""
    counts: dict[str, int] = {}
    for match in evidence.matches:
        counts[match.classification.value] = counts.get(match.classification.value, 0) + 1
    return counts


def _print_screen(result: ScreenResult) -> None:
    """Print a screening verdict: gates first, then gaps, then the tally."""
    spec, evidence = result.spec, result.evidence
    _print_screen_header(result)
    _print_gates(spec, evidence)
    _print_screen_findings(spec, evidence)
    _print_reading(result.reading, spec)
    _print_screen_tally(result)


def _print_screen_header(result: ScreenResult) -> None:
    spec = result.spec
    terminal.plain("")
    terminal.plain(f"{spec.title} at {spec.company or 'an unnamed employer'}")
    if spec.location:
        terminal.plain(f"  {spec.location}")
    terminal.plain(f"  read {result.jd.summary()}")


def _print_gates(spec: JobSpec, evidence: EvidenceMap) -> None:
    """Print each stated deal-breaker and how the evidence answers it."""
    terminal.plain("")
    if not spec.hard_gates:
        terminal.plain("The posting states no deal-breakers.")
        return

    from tailorcv.application.tailoring.stages.match import answered_gates

    terminal.plain("Deal-breakers the posting states:")
    terminal.plain("  ! the posting states it absolutely, - it states it less firmly")
    answers = answered_gates(spec, evidence)
    for answer in answers:
        marker = "!" if answer.verified_blocking else "-"
        terminal.plain(f"  {marker} {gate_kind_label(answer.kind)}: {answer.text}")
        terminal.plain(f"      answered by {answer.summary}")
        if not answer.verified:
            terminal.plain("      quoted wording is not in the posting")
    _print_gate_warnings(answers)


def _print_gate_warnings(answers: Sequence[GateAnswer]) -> None:
    """Warn about blocking conditions the record leaves without direct evidence, or unquotable."""
    unmet = [
        answer
        for answer in answers
        if answer.verified_blocking and not answer.directly_met and answer.answered_by_record
    ]
    if unmet:
        terminal.plain("")
        terminal.warning(
            f"{len(unmet)} absolute condition(s) not answered by direct evidence: "
            + ", ".join(gate_kind_label(answer.kind) for answer in unmet)
        )
    unverified = [answer for answer in answers if not answer.verified]
    if unverified:
        terminal.warning(
            f"{len(unverified)} condition(s) quote wording the posting does not contain: "
            + ", ".join(gate_kind_label(answer.kind) for answer in unverified)
        )


def _print_requirements(spec: JobSpec, matches: Sequence[Match]) -> None:
    for match in matches:
        requirement = spec.requirement(match.requirement_id)
        if requirement is not None:
            terminal.plain(f"  - {requirement.text}")


def _print_screen_findings(spec: JobSpec, evidence: EvidenceMap) -> None:
    """Print unsupported, unestablished, and self-asserted requirements in that order."""
    total = len(spec.requirements)
    gaps = evidence.gaps()
    terminal.plain("")
    if gaps:
        terminal.warning(f"{len(gaps)} requirement(s) your CV does not support:")
        _print_requirements(spec, gaps)
    elif not evidence.needs_review():
        terminal.success(f"All {total} extracted requirements have supporting evidence.")

    unresolved = evidence.needs_review()
    if unresolved:
        # A run that could not tell is not a finding about the CV.
        terminal.plain("")
        terminal.warning(f"{len(unresolved)} requirement(s) this run could not establish:")
        _print_requirements(spec, unresolved)

    asserted = evidence.asserted()
    if asserted:
        terminal.plain("")
        terminal.plain(f"{len(asserted)} answered by what you state rather than by evidence:")
        _print_requirements(spec, asserted)


def _print_screen_tally(result: ScreenResult) -> None:
    """Print the requirement count, the cost, and what to run next."""
    from tailorcv.application.tailoring.stages.match import Classification

    counts = _classification_counts(result.evidence)
    direct = counts.get(Classification.DIRECT.value, 0)
    terminal.plain("")
    terminal.plain(
        f"{len(result.spec.requirements)} requirements, {direct} answered directly. "
        f"Cost: {result.ledger.total_spent()}."
    )
    if result.report is not None:
        terminal.plain(f"  {result.report}")
    terminal.plain("")
    terminal.plain("To write a resume for it: tailorcv run <the same posting>")


def _print_reading(reading: Reading, spec: JobSpec) -> None:
    """Print what the rules read out of the posting, separately from the model.

    Printed in full, abstentions included.
    """
    terminal.plain("")
    terminal.plain("Read from the posting text by rule, not by a model. Not a score:")
    for answer in reading.answers():
        terminal.plain(f"  {answer.question:<38} {answer.render()}")

    from tailorcv.application.tailoring.fit import disagreements as rule_disagreements

    disagreements = rule_disagreements(reading, spec)
    if not disagreements:
        return
    terminal.plain("")
    terminal.warning(
        f"The model and these rules disagree on {len(disagreements)} thing(s). "
        "One of the two is wrong:"
    )
    for item in disagreements:
        terminal.plain(f"  - {item.subject}: model {item.model_says}, rules {item.rules_say}")


def _print_batch(result: Batch, written: Path | None) -> None:
    """Print one line per posting, then the shared gaps, then what to do next.

    Postings follow the person's own priority and then the order they typed,
    never a derived ranking.
    """
    from tailorcv.application.tailoring.batch import Verdict

    terminal.plain("")
    terminal.plain(f"{len(result.screenings)} posting(s), screened separately. Nothing drafted.")
    for item in result.ordered():
        recommendation = result.recommendations[item.key]
        show = {
            Verdict.PURSUE: terminal.success,
            Verdict.CAUTIOUS: terminal.warning,
            Verdict.SKIP: terminal.error,
        }[recommendation.verdict]
        terminal.plain("")
        show(f"{recommendation.verdict.value:<16} {item.spec.title} at {item.spec.company or '?'}")
        for reason in recommendation.reasons:
            terminal.plain(f"    - {reason}")

    terminal.plain("")
    if result.opportunities:
        terminal.plain("Gaps ranked by how many of these postings one closed gap would help:")
        for cluster in result.opportunities:
            terminal.plain(f"  {cluster.leverage} x  {cluster.subject}")
    else:
        terminal.plain("Nothing in this batch is unanswered by your record.")

    terminal.plain("")
    terminal.plain(
        "None of the above is a score. Each recommendation is a summary of the reasons\n"
        "printed beside it, and every reason quotes the posting or your record."
    )
    if written is not None:
        record = written.parent / "batch.json"
        terminal.plain("")
        terminal.plain(f"  {written}")
        terminal.plain(f"  {record}")
        terminal.plain("")
        terminal.plain(f"To work through the shared gaps: tailorcv discover --queue {record}")


def _print_discovery(outcome: Outcome) -> None:
    """Print what a discovery session found, wrote, and left open."""
    terminal.plain("")
    answered = sum(1 for line in outcome.lines if line.answered)
    terminal.plain(f"{len(outcome.lines)} subject(s) asked about, {answered} with an answer.")
    for refusal in outcome.refusals:
        terminal.plain("")
        terminal.warning(f"Not recorded: {refusal}")
    if outcome.written is not None:
        terminal.plain("")
        terminal.success(
            f"Added {len(outcome.written.entry_ids)} confirmed fact(s) to "
            f"{outcome.written.master_cv}."
        )
        terminal.plain(f"  {', '.join(outcome.written.entry_ids)}")
        terminal.plain(f"  {outcome.written.journal}  where each of them came from")
    elif outcome.confirmed:
        terminal.plain("")
        terminal.plain("Nothing was written, so the master CV is unchanged.")
    if outcome.unclosed:
        terminal.plain("")
        terminal.warning(
            f"{len(outcome.unclosed)} gap(s) are still gaps: {', '.join(outcome.unclosed)}.\n"
            "  Discovery did not close them, so they are still reported as gaps."
        )


def _materialized(result: RunResult) -> MaterializedRun:
    materialized = result.materialized
    if materialized is None:
        raise ConfigError("The CLI cannot print a tailoring run that was not materialized.")
    return materialized


def _print_run(result: RunResult, verbose: bool) -> None:
    """Print the run summary, most important thing first.

    Whether it worked and where the file is come first; per-check detail and
    the token ledger print only when verbose.
    """
    materialized = _materialized(result)
    _print_run_verdict(result, verbose)
    _print_run_files(materialized)
    _print_run_cost(result, verbose)
    _print_run_disagreements(result)

    for note in result.notes:
        terminal.plain("")
        terminal.plain(note)
    for warning in result.warnings:
        terminal.plain("")
        terminal.warning(warning)


def _print_run_verdict(result: RunResult, verbose: bool) -> None:
    """Print whether the run is sendable, then the failed checks or all of them."""
    findings = result.verification.findings
    failures = result.verification.failures
    passed = sum(finding.outcome is results.Outcome.PASS for finding in findings)
    warned = sum(finding.outcome is results.Outcome.WARN for finding in findings)

    terminal.plain("")
    if result.reused:
        # Reused runs report their manifest count, not an empty findings list.
        terminal.success("Done. Returned the run already on disk.")
    elif failures:
        terminal.error(
            f"Not sendable. {len(failures)} of {len(findings)} checks failed "
            f"after {result.attempts} attempt(s)."
        )
        terminal.plain("")
        for finding in failures:
            terminal.finding(finding)
    elif warned or result.verification.critic is None:
        unread = result.verification.critic is None
        terminal.success(
            f"Done. {passed} passed, {warned} warned"
            f"{'; the critic did not read the resume' if unread else ''}."
        )
    else:
        terminal.success(f"Done. All {passed} checks passed.")

    if verbose:
        terminal.plain("")
        for finding in findings:
            terminal.finding(finding)


def _print_run_files(materialized: MaterializedRun) -> None:
    terminal.plain("")
    terminal.plain(f"  {materialized.directory}")
    terminal.plain(f"    {materialized.document.name:<32} the resume")
    if materialized.cover_letter is not None:
        terminal.plain(f"    {materialized.cover_letter.name:<32} the cover letter")
    terminal.plain(
        f"    {materialized.report.name:<32} read this first: gates, gaps, and provenance"
    )
    if materialized.review is not None:
        terminal.plain(f"    {'changes.md':<32} every proposed line and what it rests on")


def _print_run_cost(result: RunResult, verbose: bool) -> None:
    terminal.plain("")
    terminal.plain(f"  Cost: {result.ledger.total_spent()}. Full breakdown in the report.")
    if verbose:
        terminal.plain("")
        for line in result.ledger.summary_lines():
            terminal.plain(f"  {line}")


def _print_run_disagreements(result: RunResult) -> None:
    """Warn when the rules read the posting differently from the model."""
    if result.reading is None:
        return
    from tailorcv.application.tailoring.fit import disagreements as rule_disagreements

    disagreements = rule_disagreements(result.reading, result.spec)
    if not disagreements:
        return
    terminal.plain("")
    terminal.warning(
        f"The model and the deterministic rules disagree on {len(disagreements)} "
        "thing(s):\n  "
        + "; ".join(
            f"{item.subject} (model {item.model_says}, rules {item.rules_say})"
            for item in disagreements[:3]
        )
        + "\n  Read the report section 'Read from the text by rule'."
    )
