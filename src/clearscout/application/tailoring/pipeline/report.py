"""Assemble decision reports from stored evaluations and provenance."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.prompts import suite_fingerprint
from clearscout.application.tailoring.fit import Reading, disagreements
from clearscout.application.tailoring.layout import Budget
from clearscout.application.tailoring.runs import MANIFEST, input_fingerprints, settings_hash
from clearscout.application.tailoring.stages.draft import Fitted, ResumeDraft
from clearscout.application.tailoring.stages.jobspec import JobSpec, StoredPostingName
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    answered_gates,
    example_list_notes,
)
from clearscout.application.tailoring.stages.select import Plan
from clearscout.application.tailoring.stages.verify import (
    CRITIC_CHECK,
    LETTER_CRITIC,
    CriticVerdict,
    DoubtedClaim,
    Report,
    id_texts,
    worded,
)
from clearscout.domain.requirements import gate_kind_label
from clearscout.infrastructure.artifacts.files import write_private
from clearscout.infrastructure.build import current_build
from clearscout.infrastructure.providers.credentials import Credential
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.shared.costs import Ledger
from clearscout.shared.results import Outcome, table_cell

from .models import StoredMasterCv
from .rewrite import (
    WrittenLetter,
    cap_notes,
    overflow_warning,
    publication_notes,
    short_page,
    unprinted_earlier_career,
)

TRUNCATE = 160
EXCERPT = 80


def render_report(
    *,
    jd: JDText,
    spec: JobSpec,
    evidence: EvidenceMap,
    plan: Plan,
    fitted: Fitted,
    verification: Report,
    inventory: CvInventory,
    settings: Settings,
    ledger: Ledger,
    credential: Credential,
    reading: Reading,
    today: date | None = None,
    previous: Path | None = None,
    letter: WrittenLetter | None = None,
    stored_cv: StoredMasterCv | None = None,
    stored_name: StoredPostingName | None = None,
) -> str:
    """Render the exact text of ``Tailoring_Report.md``."""
    known = id_texts(spec, inventory, letter.research if letter else ())
    shown = verification.for_reader(known)
    sections = (
        _header(spec, jd, settings, inventory, credential, today, stored_cv, stored_name),
        _review_priorities(shown),
        _gates(spec, evidence),
        _gaps(spec, evidence),
        _unestablished(spec, evidence),
        _discarded(evidence),
        _derived(reading, spec),
        _requirements(spec, evidence, inventory),
        _named_tools(spec, evidence, inventory),
        _example_notes(spec, evidence),
        _coverage(verification),
        _since(
            previous,
            input_fingerprints(inventory.content_hash, settings),
            verification.coverage.ratio * 100,
        ),
        _checks(shown),
        _critic(verification, letter, known),
        _length(settings, plan, inventory, fitted),
        _provenance(fitted.printed, inventory),
        _letter_provenance(letter, inventory),
        _costs(ledger),
    )
    return "\n\n".join(part for part in sections if part).strip() + "\n"


def _review_priorities(verification: Report) -> str:
    """Put actionable omissions and the limits of verification before the full ledger."""
    findings = [
        item
        for item in verification.findings
        if item.outcome is not Outcome.PASS or item.advisories
    ]
    lines = [
        "## Application readiness",
        "",
        "- Structural checks: " + ("no recorded failures." if verification.passed else "failed."),
        "- Factual support: compare ownership, qualifications, dates, and metrics "
        "with the sources.",
        "- Human approval: inspect the decisions bound to this exact package before using it.",
        "- Layout: unconfirmed. Render the final resume and check page count and legibility.",
    ]
    if findings:
        lines += ["", "Review these before the full evidence ledger:", ""]
        for item in findings:
            lines += [f"- {item.line()}", *(f"  - {note}" for note in item.advisories)]
    return "\n".join(lines)


def _since(previous: Path | None, current: dict[str, str], coverage: float) -> str:
    """Say what moved since the last run of this posting, or nothing at all.

    A person re-running a posting after editing their CV wants one question
    answered: did that help.
    """
    if previous is None:
        return ""
    manifest = previous / MANIFEST
    if not manifest.is_file():
        return ""
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""

    inputs = payload.get("inputs", {})
    names = {
        "cv_hash": "the master CV",
        "prompt_suite": "the prompts",
        "settings_hash": "the settings",
    }
    changed = [label for key, label in names.items() if inputs.get(key) != current.get(key)]
    lines = [
        "## Since the previous run",
        "",
        f"Compared against `{previous.name}`, run {payload.get('run', {}).get('started', '?')}.",
        "",
        "Inputs that changed: " + ", ".join(changed) if changed else "No input changed.",
    ]
    earlier = _previous_coverage(payload)
    if earlier is not None:
        direction = "unchanged at" if abs(earlier - coverage) < 0.5 else f"{earlier:.0f}% to"
        lines += ["", f"Keyword coverage {direction} {coverage:.0f}%."]
    return "\n".join(lines)


def _previous_coverage(manifest: dict[str, object]) -> float | None:
    """Return a valid earlier coverage value, or no comparison for malformed input."""
    run_record = manifest.get("run")
    value = run_record.get("coverage") if isinstance(run_record, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _table(title: str, preamble: Iterable[str], headers: Iterable[str], rows: Iterable[str]) -> str:
    """Render one titled Markdown table with an optional explanatory preamble."""
    columns = list(headers)
    return "\n".join(
        [
            f"## {title}",
            "",
            *preamble,
            "",
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
            *rows,
        ]
    )


def _header(
    spec: JobSpec,
    jd: JDText,
    settings: Settings,
    inventory: CvInventory,
    credential: Credential,
    today: date | None,
    stored_cv: StoredMasterCv | None = None,
    stored_name: StoredPostingName | None = None,
) -> str:
    """Return the run's provenance: what was read, when, and under which rules."""
    master = str(inventory.source) if stored_cv is None else stored_cv.describe()
    employer = spec.company or "not stated"
    if stored_name is not None and stored_name.employer_from_address_only:
        employer = (
            f'The employer name "{stored_name.employer}" comes only from the posting\'s '
            "address and the posting text never spells it, so this package names no employer."
        )
    return "\n".join(
        [
            f"# Tailoring report: {spec.title}",
            "",
            f"- Employer: {employer}",
            f"- Posting: {jd.summary()}",
            f"- Seniority signal: {spec.seniority or 'not stated'}",
            f"- Posting location: {spec.location or 'not stated'}",
            f"- Run date: {(today or datetime.now(UTC).date()).isoformat()}",
            f"- Master CV: {master} (content hash {inventory.content_hash})",
            f"- Effective settings: {settings_hash(settings)} "
            f"(configuration source: {settings.source or 'runtime/workspace settings'})",
            f"- Code build: {current_build().revision or 'unknown'}",
            f"- Package version: {current_build().package_version}",
            f"- Rules: {'in force' if settings.rules.configured else 'none configured'}",
            f"- Prompt suite: {suite_fingerprint()}",
            f"- Credentials: {credential.describe()}",
            f"- Provider: {settings.provider.value}, page budget {settings.pages}",
        ]
    )


def _gates(spec: JobSpec, evidence: EvidenceMap) -> str:
    """Return the posting's stated conditions and how the record answers each."""
    if not spec.hard_gates:
        return "## Hard gates\n\nThe posting states none."
    answers = answered_gates(spec, evidence)
    unmet = [
        answer
        for answer in answers
        if answer.blocking and not answer.directly_met and answer.answered_by_record
    ]
    unverified = [answer for answer in answers if not answer.verified]
    notes = ["Conditions the posting states. Check these before spending time on the rest."]
    if unmet:
        notes.append(
            f"**{len(unmet)} absolute condition(s) are not answered by direct evidence.** "
            "Decide whether the record really meets them before spending an afternoon."
        )
    if unverified:
        notes.append(
            f"**{len(unverified)} condition(s) quote wording this posting does not contain.** "
            "Read the posting itself before relying on them."
        )
    return _table(
        "Hard gates",
        notes,
        ("Kind", "Stated", "Absolute", "Source", "Answered by"),
        [
            f"| {gate_kind_label(answer.kind)} | {_cell(answer.text)} | "
            f"{'yes' if answer.verified_blocking else 'unclear'} | "
            f"{answer.span_status.value} | {answer.summary} |"
            for answer in answers
        ],
    )


def _gaps(spec: JobSpec, evidence: EvidenceMap) -> str:
    """Return the requirements the master CV does not support."""
    gaps = evidence.gaps()
    if not gaps:
        return "## Gaps\n\nEvery extracted requirement has supporting evidence."
    rows = [
        f"| {requirement.id} | {requirement.importance.value} | "
        f"{_cell(requirement.text)} | {_cell(match.note)} |"
        for match in gaps
        if (requirement := spec.requirement(match.requirement_id))
    ]
    return _table(
        "Gaps",
        ["Requirements the master CV does not support. These are not papered over."],
        ("ID", "Importance", "Requirement", "Note"),
        rows,
    )


def _discarded(evidence: EvidenceMap) -> str:
    """Return what the model answered that this posting never asked."""
    if not evidence.unrecognized:
        return ""
    return "\n".join(
        [
            "## Discarded answers",
            "",
            "The model answered "
            + ", ".join(evidence.unrecognized)
            + ", which this posting does not have. Those answers were discarded, so any "
            "requirement they were meant for is reported above as a gap.",
        ]
    )


def _unestablished(spec: JobSpec, evidence: EvidenceMap) -> str:
    """Return the requirements this run could not settle either way."""
    unresolved = evidence.needs_review()
    if not unresolved:
        return ""
    rows = [
        f"| {requirement.id} | {requirement.importance.value} | "
        f"{_cell(requirement.text)} | {_cell(match.note)} |"
        for match in unresolved
        if (requirement := spec.requirement(match.requirement_id))
    ]
    return _table(
        "Needs review",
        [
            "The run did not establish these either way, so they are not gaps and "
            "not answered. Re-run the analysis, or read the master CV against them."
        ],
        ("ID", "Importance", "Requirement", "Note"),
        rows,
    )


def _requirements(spec: JobSpec, evidence: EvidenceMap, inventory: CvInventory) -> str:
    """Return every requirement, its classification, and the evidence cited."""
    rows = []
    for requirement in spec.requirements:
        match = evidence.for_requirement(requirement.id)
        classification = match.classification.value if match else Classification.GAP.value
        strength = match.strength.value if match else "none"
        cited = ", ".join(match.entry_ids) if match and match.entry_ids else "-"
        rows.append(
            f"| {requirement.id} | {requirement.importance.value} | "
            f"{_cell(requirement.text)} | {classification} | {strength} | {cited} |"
        )
    counts: dict[str, int] = {}
    for match in evidence.matches:
        counts[match.classification.value] = counts.get(match.classification.value, 0) + 1
    tally = ", ".join(f"{value} {key}" for key, value in sorted(counts.items())) or "none"
    return _table(
        "Requirement map",
        [
            f"{len(spec.requirements)} requirements extracted: {tally}.",
            f"Evidence drawn from {len(inventory.entries)} citable items in the master CV.",
        ],
        ("ID", "Importance", "Requirement", "Class", "Strength", "Evidence"),
        rows,
    )


def named_tool_notes(spec: JobSpec, evidence: EvidenceMap, inventory: CvInventory) -> list[str]:
    """Return one advisory per requirement whose cited evidence never names a tool it names."""
    return [
        f'No evidence cited for "{requirement.text}" names {", ".join(tools)}.'
        for requirement, tools in evidence.unnamed_tools(spec, inventory)
    ]


def _named_tools(spec: JobSpec, evidence: EvidenceMap, inventory: CvInventory) -> str:
    """Return the requirements answered by evidence that never names their tools, and any drops."""
    advisories = named_tool_notes(spec, evidence, inventory)
    dropped = []
    for requirement_id, tool in evidence.dropped_tools:
        requirement = spec.requirement(requirement_id)
        named = requirement.text if requirement else requirement_id
        dropped.append(
            f'- {tool}, listed for "{named}", is not in that requirement\'s text, '
            "so it was set aside."
        )
    if not advisories and not dropped:
        return ""
    lines = ["## Named tools"]
    if advisories:
        lines += [
            "",
            "Advisory. Each requirement below names a tool, and the evidence cited for it "
            "never does. Check whether that work used the tool, or one of its ecosystem, "
            "before an application claims it.",
            "",
            *(f"- {note}" for note in advisories),
        ]
    if dropped:
        lines += ["", *dropped]
    return "\n".join(lines)


def _example_notes(spec: JobSpec, evidence: EvidenceMap) -> str:
    """Return the requirements whose notes hold an unrequired item against the record."""
    advisories = example_list_notes(spec, evidence)
    if not advisories:
        return ""
    return "\n".join(
        [
            "## Example-list notes",
            "",
            "Advisory. A listed example, and the employer's own name or offering, is not "
            "something the ask requires, so its absence should lower neither the class nor "
            "the strength. Check each requirement below before relying on its class and "
            "strength.",
            "",
            *(f"- {note}" for note in advisories),
        ]
    )


def _derived(reading: Reading, spec: JobSpec) -> str:
    """Return what the rules read out of the posting, and where they disagree.

    Its own section, since every other section came from a model.
    """
    total = len(reading.answers())
    lines = [
        "## Read from the text by rule",
        "",
        "Derived from the posting text by rules, not by a model. It is not a score, not a",
        "grade, and not a prediction about an application: it reports what the text states",
        "and what it does not.",
        "",
        f"{reading.abstentions} of {total} questions were not answered by this posting.",
        '"not stated" is the finding, not a failure to produce one, and those rows are the',
        "ones worth a minute of your own reading.",
        "",
        "| Question | Answer | Read from |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {answer.question} | {_cell(answer.render())} | {_cell(answer.because())} |"
        for answer in reading.answers()
    ]
    return "\n".join([*lines, "", _disagreements(reading, spec)])


def _disagreements(reading: Reading, spec: JobSpec) -> str:
    """Return every place the model's extraction and the rules do not match.

    One of the two is wrong in each row, and which one is a question a person
    answers by looking at the posting. Neither overrides the other here.
    """
    found = disagreements(reading, spec)
    if not found:
        return (
            "### Model and rules\n\n"
            "They agree on everything both of them answered. That is not a guarantee that "
            "either is right,\nonly that they did not contradict each other."
        )
    return "\n".join(
        [
            "### Where the model and the rules disagree",
            "",
            f"{len(found)} disagreement(s). One of the two readings is wrong in each row, and "
            "the tool does not\nknow which, so nothing here has been resolved on your behalf.",
            "",
            "| Subject | The model read | The rules read | Where the rules looked |",
            "| --- | --- | --- | --- |",
            *(
                f"| {item.subject} | {_cell(item.model_says)} | {_cell(item.rules_say)} "
                f"| {_cell(item.detail)} |"
                for item in found
            ),
        ]
    )


def _coverage(verification: Report) -> str:
    """Return the measured keyword coverage and what is missing."""
    coverage = verification.coverage
    total = len(coverage.present) + len(coverage.missing)
    return "\n".join(
        [
            "## Keyword coverage",
            "",
            f"{coverage.ratio * 100:.0f}% of the posting's extracted phrases appear in the "
            f"derivative ({len(coverage.present)} of {total}).",
            "",
            f"Missing: {', '.join(coverage.missing) if coverage.missing else 'none'}",
            "",
            "Coverage is measured, not targeted. No published evidence ties a coverage",
            "percentage to an interview, so a missing phrase is a prompt to check whether",
            "the master CV genuinely supports it, never a reason to add a claim.",
        ]
    )


def _checks(verification: Report) -> str:
    """Return every programmatic check and its verdict."""
    return _table(
        "Checks",
        [],
        ("Check", "Result", "Detail"),
        [
            f"| {finding.check} | {finding.outcome.value} | {_cell(finding.detail)} |"
            for finding in verification.findings
        ],
    )


def _critic(verification: Report, letter: WrittenLetter | None, known: Mapping[str, str]) -> str:
    """Return each document's critic verdict: its factual gate and quality scores."""
    verdicts = [("Resume", CRITIC_CHECK, verification)]
    if letter is not None:
        verdicts.append(("Cover letter", LETTER_CRITIC, letter.verification))
    lines = ["## Critic"]
    for document, check, checked in verdicts:
        lines += ["", f"### {document}", ""]
        lines += (
            [_unread(checked, document, check)]
            if checked.critic is None
            else _verdict(checked.critic, known, checked.critic_draws)
        )
    return "\n".join(lines)


def _unread(checked: Report, document: str, check: str) -> str:
    """Say why the critic left a document unread, in the words its warning recorded."""
    return next(
        (finding.detail for finding in checked.findings if finding.check == check),
        f"The critic did not read the {document.lower()}.",
    )


def _verdict(
    verdict: CriticVerdict, known: Mapping[str, str], draws: tuple[CriticVerdict, ...] = ()
) -> list[str]:
    """Return one critic verdict's scores, findings, and guidance as report lines."""
    lines = [
        f"- Job alignment: {verdict.alignment}/5",
        f"- Content preservation: {verdict.preservation}/5",
        f"- Specificity: {verdict.specificity}/5",
        f"- Recruiter readability: {verdict.readability}/5",
        f"- ATS clarity: {verdict.ats_clarity}/5",
        f"- Human voice: {verdict.human_voice}/5",
        f"- Hiring-quality mean: {verdict.quality_average:.1f}/5",
    ]
    lines += _draws(draws)
    for title, items in (
        # The boundary a doubt crosses is vocabulary the model and the redraft loop
        # share, so the document carries the claim the person has to judge.
        (
            "Unsupported claims",
            [worded(_doubt(doubt, len(draws)), known) for doubt in verdict.unsupported_claims],
        ),
        ("Style flags", [worded(item, known) for item in verdict.style_flags]),
        ("Weaknesses", [worded(item, known) for item in verdict.weaknesses]),
    ):
        if items:
            lines += ["", f"{title}:", "", *[f"- {item}" for item in items]]
    if verdict.guidance:
        lines += ["", worded(verdict.guidance, known)]
    return lines


def _draws(draws: tuple[CriticVerdict, ...]) -> list[str]:
    """Return every sampled draw's scores, so the merged number shows its spread."""
    if len(draws) < 2:
        return []
    axes = list(draws[0].scores())
    headers = ["Draw", *(axis.replace("_", " ") for axis in axes)]
    return [
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *(
            f"| {number} | " + " | ".join(str(score) for score in draw.scores().values()) + " |"
            for number, draw in enumerate(draws, 1)
        ),
    ]


def _doubt(doubt: DoubtedClaim, draws: int) -> str:
    """Return one doubted claim, saying how many draws raised it when several were taken."""
    if draws < 2 or not doubt.corroboration:
        return doubt.claim
    return f"{doubt.claim} (raised by {doubt.corroboration} of {draws} draws)"


def _length(settings: Settings, plan: Plan, inventory: CvInventory, fitted: Fitted) -> str:
    """Return the estimated fill, its honesty caveat, and what to cut first.

    clearscout does not render, so it cannot report a page count. It can report
    how full its line model reads the draft and which items it gives up first.
    """
    budget = Budget(settings.pages)
    lines = [
        "## Length",
        "",
        f"The resume is estimated at {fitted.total_lines:.0f} of the "
        f"{budget.total_lines:.0f} lines {settings.pages} page(s) hold, with page "
        f"{settings.pages} about {budget.last_page_share(fitted.total_lines):.0%} full.",
        "clearscout does not render, so this is an estimate, not a measurement. Render the",
        "Markdown to get the real page count.",
        "",
    ]
    notes = (
        overflow_warning(settings, fitted),
        short_page(settings, plan, fitted),
        *cap_notes(settings, plan, inventory, fitted),
        *publication_notes(settings, plan, inventory, fitted),
        unprinted_earlier_career(fitted),
    )
    for note in notes:
        if note:
            lines += [" ".join(part.strip() for part in note.splitlines()), ""]
    if fitted.trimmed:
        lines += [
            f"{len(fitted.trimmed)} item(s) were trimmed in drop order after drafting to fit "
            "the page:",
            "",
            *_drop_table(fitted.trimmed, inventory, 1),
            "",
        ]
    if fitted.requested:
        lines += [
            f"{len(fitted.requested)} item(s) were dropped in drop order at your request:",
            "",
            *_drop_table(fitted.requested, inventory, 1),
            "",
        ]
    if not fitted.remaining:
        lines.append(
            "Nothing further can be dropped without losing a must-have answer or a role's "
            "last bullets."
        )
        return "\n".join(lines)
    lines += [
        "If it runs long, re-run with `--trim N` to drop this many items, in this order:",
        "",
        *_drop_table(fitted.remaining, inventory, len(fitted.requested) + 1),
    ]
    return "\n".join(lines)


def _drop_table(ids: Iterable[str], inventory: CvInventory, first: int) -> list[str]:
    """Return a table of entries in drop order, numbered from ``first``, with their text."""
    rows = ["| Order | Evidence | Text |", "| --- | --- | --- |"]
    for position, entry_id in enumerate(ids, first):
        try:
            text = inventory.by_id(entry_id).text
        except KeyError:  # pragma: no cover - plan ids come from the inventory
            text = "(not found)"
        rows.append(f"| {position} | {entry_id} | {_cell(text)} |")
    return rows


def _provenance(draft: ResumeDraft, inventory: CvInventory) -> str:
    """Return every output line and the master CV text it derives from."""
    rows = []
    for line in draft.lines():
        for entry_id in line.source_ids or ("-",):
            try:
                source = inventory.by_id(entry_id).text
            except KeyError:
                source = "(identifier not found in the master CV)"
            rows.append(f"| {_cell(line.text)} | {entry_id} | {_cell(source)} |")
    return _table(
        "Provenance",
        [
            "Every line of the derivative and the master CV text it derives from.",
            "This is the table to read before an interview.",
        ],
        ("Derivative line", "Evidence", "Master CV text"),
        rows,
    )


def _letter_provenance(written: WrittenLetter | None, inventory: CvInventory) -> str:
    """Return each letter paragraph, the identifiers it cites, and their text."""
    if written is None:
        return ""
    company = {source.id: source.text for source in written.research}
    rows = []
    for number, paragraph in enumerate(written.letter.paragraphs, 1):
        for source_id in paragraph.source_ids or ("-",):
            try:
                source = inventory.by_id(source_id).text
            except KeyError:
                source = company.get(source_id, "(identifier not found in the sources)")
            rows.append(
                f"| {number} | {source_id} | {_excerpt(source)} | {_excerpt(paragraph.text)} |"
            )
    return _table(
        "Letter provenance",
        ["Every cover letter paragraph, each identifier it cites, and that source's text."],
        ("Paragraph", "Evidence", "Source text", "Paragraph text"),
        rows,
    )


def _excerpt(text: str) -> str:
    """Return a short single-line excerpt for a provenance cell."""
    return table_cell(text, EXCERPT)


def _costs(ledger: Ledger) -> str:
    """Return the per-stage cost summary, in full regardless of terminal width."""
    return "\n".join(["## Cost", "", "```text", *ledger.summary_lines(wide=True), "```"])


def _cell(text: str) -> str:
    """Flatten one report cell, which may be long enough to need truncating."""
    return table_cell(text, TRUNCATE)


def write_screen_report(
    directory: Path,
    *,
    jd: JDText,
    spec: JobSpec,
    evidence: EvidenceMap,
    inventory: CvInventory,
    settings: Settings,
    ledger: Ledger,
    credential: Credential,
    reading: Reading,
    today: date | None = None,
) -> Path:
    """Write ``Screening_Report.md``: is this posting worth an afternoon.

    The same sections as a full report, minus the ones about a resume that was
    deliberately not written.
    """
    sections = (
        _header(spec, jd, settings, inventory, credential, today).replace(
            "# Tailoring report", "# Screening report"
        ),
        _gates(spec, evidence),
        _gaps(spec, evidence),
        _unestablished(spec, evidence),
        _discarded(evidence),
        _derived(reading, spec),
        _requirements(spec, evidence, inventory),
        _costs(ledger),
        "Nothing was drafted. Run `clearscout run` on the same posting to write a resume;\n"
        "these two stages are cached, so it will not pay for them twice.",
    )
    body = "\n\n".join(part for part in sections if part).strip() + "\n"
    return write_private(directory / "Screening_Report.md", body)
