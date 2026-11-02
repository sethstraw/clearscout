"""Render durable fit reports from the structured findings committed with an evaluation."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from tailorcv.application.contracts import (
    RULE_REFUSAL_PROMPT_FINGERPRINT,
    EvaluationCommit,
    ReportRequirementInput,
)
from tailorcv.application.job_search.evaluation_requirements import (
    merge_hard_gates_into_requirements,
)
from tailorcv.shared.results import table_cell


def render_fit_report(
    proposal: EvaluationCommit,
    *,
    evidence_ids: dict[str, str],
    weights: dict[str, Decimal],
    weight_set_name: str,
    weight_set_version: int,
) -> str:
    """Return the one-time Markdown snapshot; viewing it never calls a model."""
    report = proposal.fit_report
    cv_hash = hashlib.sha256(proposal.candidate_record.content.encode("utf-8")).hexdigest()
    score_lines = []
    for component, finding in proposal.scores.named().items():
        product = Decimal(finding.value) * weights[component] if finding.value is not None else None
        score_lines.append(
            "| "
            + " | ".join(
                (
                    component,
                    str(finding.value) if finding.value is not None else "not stated",
                    str(weights[component]),
                    f"{product:.2f}" if product is not None else "not stated",
                    table_cell(finding.reason or "supported by linked evidence"),
                )
            )
            + " |"
        )
    overall = proposal.scores.overall(weights)
    requirements = merge_hard_gates_into_requirements(
        hard_gates=report.hard_gates,
        requirements=report.requirements,
    ).rows
    gate_rows = [item for item in requirements if item.is_hard_gate]
    plain_rows = [item for item in requirements if not item.is_hard_gate]
    sections = [
        "\n".join(
            (
                f"# Fit report: {proposal.title}",
                "",
                f"- Employer: {proposal.employer}",
                f"- Evaluation date: {proposal.evaluated_at.date().isoformat()}",
                f"- Prompt suite: {report.prompt_fingerprint}",
                f"- Master CV content hash: {cv_hash}",
                f"- Score policy: {weight_set_name} v{weight_set_version}",
                f"- Decision: {proposal.decision.value}",
            )
        ),
        _table(
            "Score arithmetic",
            (
                "Component scores remain separate from hard gates. The overall is available only",
                "when every weighted component is stated.",
            ),
            ("Component", "Score", "Weight", "Product", "Finding"),
            score_lines,
        )
        + f"\n\nWeighted sum: **{overall if overall is not None else 'not stated'}**.",
        _requirement_table(
            "Hard gates",
            gate_rows,
            evidence_ids,
            none=(
                "Not read: a rule refused this posting before its requirements were extracted."
                if report.prompt_fingerprint == RULE_REFUSAL_PROMPT_FINGERPRINT
                else "The posting states none."
            ),
        ),
        _gaps(proposal, plain_rows, evidence_ids),
        _unestablished(gate_rows, plain_rows),
        _reading(proposal, evidence_ids),
        _disagreements(proposal, evidence_ids),
        _requirement_table("Requirement map", plain_rows, evidence_ids),
        _coverage(proposal),
        _changed(proposal),
        _checks(proposal),
        _critic(proposal),
        "## Length budget\n\n" + report.length_budget.strip(),
        _provenance(proposal, evidence_ids),
        _costs(proposal),
    ]
    return "\n\n".join(section.strip() for section in sections if section.strip()) + "\n"


def _requirement_table(
    title: str,
    requirements: list[ReportRequirementInput],
    evidence_ids: dict[str, str],
    *,
    none: str = "The posting states none.",
) -> str:
    if not requirements:
        return f"## {title}\n\n{none}"
    return _table(
        title,
        (),
        ("ID", "Importance", "Requirement", "Source", "Answered by"),
        [
            f"| {table_cell(item.requirement_id)} | {table_cell(item.importance)} | "
            f"{table_cell(item.text)} | {item.span_status.value} "
            f"| {_refs(item.evidence_keys, evidence_ids)} |"
            for item in requirements
        ],
    )


def _gaps(
    proposal: EvaluationCommit,
    requirements: list[ReportRequirementInput],
    evidence_ids: dict[str, str],
) -> str:
    rows = [
        f"| {table_cell(item.text)} | {_refs(item.evidence_keys, evidence_ids)} |"
        for item in proposal.gaps
    ]
    rows.extend(
        f"| {table_cell(item.requirement_id + ': ' + item.text)} | not stated |"
        for item in requirements
        if item.established and not item.evidence_keys
    )
    if not rows:
        return "## Gaps\n\nNo established requirement is unsupported."
    return _table(
        "Gaps",
        ("Unsupported requirements and recorded limits are never papered over.",),
        ("Finding", "Evidence"),
        rows,
    )


def _unestablished(
    gates: list[ReportRequirementInput], requirements: list[ReportRequirementInput]
) -> str:
    rows = [
        f"| {table_cell(item.requirement_id + ': ' + item.text)} | {state} |"
        for items, state in (
            (gates, "Hard gate the run did not settle. Decide whether you meet it."),
            (requirements, "The run did not establish it."),
        )
        for item in items
        if not item.established
    ]
    if not rows:
        return ""
    return _table(
        "Needs review",
        ("These requirements are not gaps in the master CV.",),
        ("Requirement", "State"),
        rows,
    )


def _reading(proposal: EvaluationCommit, evidence_ids: dict[str, str]) -> str:
    rows = []
    for item in proposal.fit_report.reading:
        answer = item.answer if item.answer is not None else "not stated"
        state = "conflict" if item.conflict else ("supported" if item.answer else "not stated")
        source = (
            _refs(item.evidence_keys, evidence_ids) if item.evidence_keys else table_cell(item.note)
        )
        rows.append(f"| {table_cell(item.question)} | {table_cell(answer)} | {state} | {source} |")
    return _table(
        "Read from the text by rule",
        (
            "These findings are deterministic readings, not scores. Missing and conflicting",
            "answers remain explicit.",
        ),
        ("Question", "Answer", "State", "Read from"),
        rows,
    )


def _disagreements(proposal: EvaluationCommit, evidence_ids: dict[str, str]) -> str:
    found = proposal.fit_report.disagreements
    if proposal.fit_report.prompt_fingerprint == RULE_REFUSAL_PROMPT_FINGERPRINT:
        return (
            "## Model and rules disagreements\n\n"
            "Not compared: a rule refused this posting before the model read it."
        )
    if not found:
        return "## Model and rules disagreements\n\nNo disagreement was recorded."
    return _table(
        "Model and rules disagreements",
        ("Neither side is silently chosen; each row remains a human review item.",),
        ("Subject", "Model", "Rules", "Evidence"),
        [
            f"| {table_cell(item.subject)} | {table_cell(item.model_says)} | "
            f"{table_cell(item.rules_say)} "
            f"| {_refs(item.evidence_keys, evidence_ids)} |"
            for item in found
        ],
    )


def _coverage(proposal: EvaluationCommit) -> str:
    report = proposal.fit_report
    ratio = report.coverage_present / report.coverage_total * 100 if report.coverage_total else None
    measured = (
        f"{ratio:.0f}% ({report.coverage_present} of {report.coverage_total})"
        if ratio is not None
        else "not stated (no extracted phrases)"
    )
    return "\n".join(
        (
            "## Keyword coverage",
            "",
            f"Measured coverage: {measured}.",
            "",
            "Coverage is measured, not targeted. It is a prompt to check genuine support,",
            "never a reason to add a claim.",
        )
    )


def _changed(proposal: EvaluationCommit) -> str:
    changes = proposal.fit_report.changed_since_previous
    return "## Since the previous run\n\n" + (
        "\n".join(f"- {table_cell(item)}" for item in changes)
        if changes
        else "No previous comparable run was supplied."
    )


def _checks(proposal: EvaluationCommit) -> str:
    return _table(
        "Checks",
        (),
        ("Check", "Result", "Detail"),
        [
            f"| {table_cell(item.check)} | {table_cell(item.result)} | {table_cell(item.detail)} |"
            for item in proposal.fit_report.checks
        ],
    )


def _critic(proposal: EvaluationCommit) -> str:
    scores = proposal.fit_report.critic_scores
    if not scores:
        return "## Critic scores\n\nThe critic did not run."
    return "\n".join(
        [
            "## Critic scores",
            "",
            *(f"- {table_cell(name)}: {value}/5" for name, value in scores.items()),
        ]
    )


def _provenance(
    proposal: EvaluationCommit,
    evidence_ids: dict[str, str],
) -> str:
    rows = []
    by_key = {item.key: item for item in proposal.score_evidence}
    for line in proposal.fit_report.provenance:
        for key in line.evidence_keys:
            source = by_key[key]
            rows.append(
                f"| {table_cell(line.derivative_line)} | {evidence_ids[key]} | "
                f"{table_cell(source.text)} |"
            )
    if not rows:
        return "## Provenance\n\nNo derivative lines were produced by this evaluation."
    return _table(
        "Provenance",
        ("Every derivative line and the exact stored source quote it derives from.",),
        ("Derivative line", "Evidence", "Source text"),
        rows,
    )


def _costs(proposal: EvaluationCommit) -> str:
    costs = proposal.fit_report.costs
    if not costs:
        return "## Cost\n\nNo provider cost was recorded."
    return _table(
        "Cost",
        ("Costs are estimates from recorded provider usage; unknown costs are not zero.",),
        ("Stage", "Model", "Input", "Output", "Amount"),
        [
            f"| {table_cell(item.stage)} | {table_cell(item.model or 'not stated')} | "
            f"{item.input_tokens} | {item.output_tokens} | "
            f"{str(item.amount) + ' ' + item.currency if item.amount is not None else 'Unknown'} |"
            for item in costs
        ],
    )


def _refs(keys: list[str], evidence_ids: dict[str, str]) -> str:
    return ", ".join(evidence_ids[key] for key in keys) if keys else "not stated"


def _table(title: str, preamble: tuple[str, ...], headers: tuple[str, ...], rows: list[str]) -> str:
    lines = [f"## {title}", "", *preamble]
    if preamble:
        lines.append("")
    lines.extend(
        (
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *rows,
        )
    )
    return "\n".join(lines)


__all__ = ["render_fit_report"]
