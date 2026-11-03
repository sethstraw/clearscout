"""Render and persist batch screening results and discovery queues."""

from __future__ import annotations

import json
from pathlib import Path

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring import elicit
from clearscout.application.tailoring.elicit import Line
from clearscout.application.tailoring.stages.match import answered_gates
from clearscout.domain.requirements import gate_kind_label
from clearscout.infrastructure.artifacts.files import write_private
from clearscout.shared.errors import ConfigError
from clearscout.shared.results import table_cell

from .analysis import CELL, Batch, Cluster, Screening, fit, gaps_of, strongest

# Another version is refused, not reinterpreted.
SCHEMA = 1
REPORT = "Batch_Report.md"
RECORD = "batch.json"


def queue(batch: Batch, limit: int = 0) -> tuple[Line, ...]:
    """Return the shared discovery queue in leverage order."""
    ordered = batch.opportunities if limit <= 0 else batch.opportunities[:limit]
    return elicit.lines_for(
        [(cluster.subject, cluster.gap_ids, cluster.postings) for cluster in ordered]
    )


def payload(batch: Batch) -> dict[str, object]:
    """Return the batch as the JSON a later command reads back."""
    return {
        "schema": SCHEMA,
        "started": batch.started,
        "postings": [
            {
                "key": item.key,
                "source": item.request.source,
                "posting_id": item.posting_id,
                "priority": item.request.priority,
                "note": item.request.note,
                "company": item.spec.company or "",
                "role": item.spec.title,
                "recommendation": batch.recommendations[item.key].verdict.value,
                "reasons": list(batch.recommendations[item.key].reasons),
            }
            for item in batch.ordered()
        ],
        "shared": [_cluster_payload(cluster) for cluster in batch.shared],
        "queue": [_cluster_payload(cluster) for cluster in batch.opportunities],
    }


def _cluster_payload(cluster: Cluster) -> dict[str, object]:
    """Return one cluster as it lands in ``batch.json``."""
    return {
        "subject": cluster.subject,
        "label": cluster.label,
        "joined_by": list(cluster.joined_by),
        "leverage": cluster.leverage,
        "postings": list(cluster.postings),
        "must_have_in": list(cluster.must_have_in),
        "blocking_in": list(cluster.blocking_in),
        "gap_ids": list(cluster.gap_ids),
        "wordings": [{"posting": key, "text": text} for key, text in cluster.wordings],
    }


def lines_from(path: Path) -> tuple[Line, ...]:
    """Read a batch record and return its discovery queue."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Cannot read the batch record {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ConfigError(
            f"{path} is not a batch record this version reads (schema {SCHEMA}).\n"
            "  Run the batch again and use the batch.json it writes."
        )
    entries = record.get("queue") or []
    return elicit.lines_for(
        [
            (
                str(entry.get("subject", "")),
                tuple(str(item) for item in entry.get("gap_ids", ())),
                tuple(str(item) for item in entry.get("postings", ())),
            )
            for entry in entries
            if isinstance(entry, dict) and entry.get("subject")
        ]
    )


def markdown(batch: Batch, inventory: CvInventory) -> str:
    """Render the batch for a person: each posting, then what they share."""
    lines = [
        "# Batch screening",
        "",
        f"{len(batch.screenings)} posting(s), screened separately, {batch.started}.",
        "",
        "Nothing here is a score. Each posting carries a recommendation and the reasons",
        "it was reached by, every reason quotes the posting or the record it was read",
        "from, and no number below is a probability. Nothing was drafted: run",
        "`clearscout run` on the postings you decide are worth it.",
    ]
    for item in batch.ordered():
        lines += _posting_markdown(item, batch, inventory)
    lines += _shared_markdown(batch)
    lines += _queue_markdown(batch)
    return "\n".join(lines).rstrip() + "\n"


def _posting_markdown(item: Screening, batch: Batch, inventory: CvInventory) -> list[str]:
    """Render one posting's section of the batch report."""
    recommendation = batch.recommendations[item.key]
    company = item.spec.company or "an unnamed employer"
    lines = [
        "",
        f"## {item.spec.title} at {company}",
        "",
        f"- Key: `{item.key}`",
        f"- Source: {item.request.source}",
        f"- Read: {item.read}",
    ]
    if item.request.priority:
        lines.append(f"- Your priority: {item.request.priority}")
    if item.request.note:
        lines.append(f"- Your note: {item.request.note}")
    lines += ["", f"**{recommendation.verdict.value}**", ""]
    lines += [f"- {reason}" for reason in recommendation.reasons]
    lines += ["", fit(item.spec, item.evidence), ""]

    lines += ["Deal-breakers the posting states:", ""]
    lines += _gate_lines(item)

    lines += ["", "Strongest evidence:", ""]
    strong = strongest(item.spec, item.evidence, inventory)
    lines += [f"- {row}" for row in strong] or ["- nothing is answered by direct evidence"]

    lines += ["", "Material gaps:", ""]
    gaps = gaps_of(item.spec, item.evidence)
    lines += [f"- {row}" for row in gaps] or ["- none"]

    lines += ["", "Shared with the rest of the batch:", ""]
    lines += _also_asked_lines(item, batch)

    lines += ["", "High-leverage discovery for this posting:", ""]
    lines += _leverage_lines(item, batch)
    return lines


def _gate_lines(item: Screening) -> list[str]:
    """Render every gate the posting states, and what answered it."""
    gates = answered_gates(item.spec, item.evidence)
    if not gates:
        return ["- none"]
    return [
        f"- {'absolute' if gate.blocking else 'stated less firmly'}, {gate_kind_label(gate.kind)}: "
        f"{table_cell(gate.text, CELL)} - answered by {gate.summary}"
        for gate in gates
    ]


def _also_asked_lines(item: Screening, batch: Batch) -> list[str]:
    """Render the requirements this posting shares with the rest of the batch."""
    with_others = [
        f"- {table_cell(cluster.label, CELL)} (also in "
        + ", ".join(key for key in cluster.postings if key != item.key)
        + ")"
        for cluster in batch.shared
        if item.key in cluster.postings
    ]
    return with_others or ["- nothing this batch also asks for"]


def _leverage_lines(item: Screening, batch: Batch) -> list[str]:
    """Render this posting's gaps that closing would also help another posting."""
    here = [
        f"- {cluster.subject} would also help {cluster.leverage - 1} other posting(s)"
        for cluster in batch.opportunities
        if item.key in cluster.postings and cluster.leverage > 1
    ]
    return here or ["- no gap of this posting is shared with another"]


def _shared_markdown(batch: Batch) -> list[str]:
    """Render what the postings ask for in common."""
    lines = [
        "",
        "## What these postings ask for in common",
        "",
        "Two requirements are the same when they share wording, or a term this batch",
        "does not use everywhere. The joining term is printed so you can disagree with it.",
        "",
    ]
    if not batch.shared:
        return [*lines, "No requirement is stated by more than one of these postings."]
    lines += ["| Postings | Joined by | Requirement |", "| --- | --- | --- |"]
    lines += [
        f"| {len(cluster.postings)} | {', '.join(cluster.joined_by) or 'wording'} | "
        f"{table_cell(cluster.label, CELL)} |"
        for cluster in batch.shared
    ]
    return lines


def _queue_markdown(batch: Batch) -> list[str]:
    """Render the shared discovery queue, highest leverage first."""
    lines = [
        "",
        "## Shared discovery queue",
        "",
        "Gaps ranked by leverage, which is how many of these postings one closed gap",
        "would help. A count of documents, not a prediction about an application.",
        "",
    ]
    if not batch.opportunities:
        return [*lines, "Nothing in this batch is unanswered by the record."]
    lines += ["| Leverage | Essential in | Subject | Stated as |", "| --- | --- | --- | --- |"]
    lines += [
        f"| {cluster.leverage} | {len(cluster.must_have_in)} | {table_cell(cluster.subject, 40)} "
        f"| {table_cell(cluster.label, CELL)} |"
        for cluster in batch.opportunities
    ]
    lines += [
        "",
        "To work through these one at a time, without inventing anything:",
        "",
        "    clearscout discover --queue batch.json",
    ]
    return lines


def write(directory: Path, batch: Batch, inventory: CvInventory) -> Path:
    """Write ``Batch_Report.md`` and ``batch.json``, and return the report path."""
    write_private(directory / RECORD, json.dumps(payload(batch), indent=2) + "\n")
    return write_private(directory / REPORT, markdown(batch, inventory))
