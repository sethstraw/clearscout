"""Pure projection of evaluation findings into the stored fit report."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring import fit
from clearscout.application.tailoring.fit import Disagreement
from clearscout.application.tailoring.stages.jobspec import (
    Dimension,
    HardGate,
    Importance,
    JobSpec,
    Requirement,
)
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.application.tailoring.stages.spans import locate
from clearscout.application.tailoring.workflows.evaluation_report import project_fit_report
from clearscout.application.tailoring.workflows.models import EvaluationOutput
from clearscout.domain import SpanStatus
from clearscout.domain.requirements import GateKind
from clearscout.domain.search_parameters import OUTSIDE_DECLARED
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.shared.costs import CallManifest, Ledger, Usage


def _output() -> EvaluationOutput:
    posting = "# Platform Engineer\n\n## Requirements\n- Python\n- Must be in Canada"
    inventory = CvInventory(source=Path("master.md"), content_hash="12345678", name="Candidate")
    ledger = Ledger()
    ledger.record(CallManifest("jobspec", "model-a", Usage(input_tokens=20, output_tokens=8)))
    ledger.record(CallManifest("match", "model-b", Usage(input_tokens=30, output_tokens=10)))
    return EvaluationOutput(
        spec=JobSpec(
            title="Platform Engineer",
            company="Example",
            seniority=None,
            location="Canada",
            requirements=(
                Requirement(
                    id="R1",
                    text="Python",
                    importance=Importance.MUST_HAVE,
                    source_span=locate("Python", posting),
                ),
                Requirement(id="R2", text="Kubernetes", importance=Importance.NICE_TO_HAVE),
            ),
            keywords=("PYTHON", "Kubernetes"),
            hard_gates=(
                HardGate(
                    kind="location",
                    text="Must be in Canada",
                    blocking=True,
                    source_span=locate("Must be in Canada", posting),
                ),
                HardGate(kind="degree", text="A degree is preferred"),
            ),
            origin="posting.txt",
        ),
        evidence=EvidenceMap(),
        inventory=inventory,
        ledger=ledger,
        jd=JDText(text=posting, origin="posting.txt", method="direct_text"),
        reading=fit.read(posting, inventory, today=date(2026, 8, 11)),
        outcome="pursue",
        reasons=("The posting was evaluated.",),
        prompt_fingerprint="prompt-suite",
    )


EDUCATION = Disagreement("Education required", "A degree is preferred", "not stated")


def test_the_fit_report_preserves_source_order_keys_coverage_checks_and_costs() -> None:
    output = _output()
    reading_keys = {
        name: [f"reading-{position}"]
        for position, (name, _) in enumerate(output.reading.named(), 1)
    }

    report = project_fit_report(
        output,
        candidate_text="Built Python services.",
        requirement_keys={"R1": ["requirement-1-candidate-1"]},
        hard_gate_evidence_keys=("hard-gate-1", "hard-gate-2"),
        reading_keys=reading_keys,
        disagreements=[(EDUCATION, ["disagreement-1-1"])],
        fallback_evidence_key="posting-body",
    )

    assert [item.model_dump() for item in report.disagreements] == [
        {
            "subject": "Education required",
            "model_says": "A degree is preferred",
            "rules_say": "not stated",
            "evidence_keys": ["disagreement-1-1"],
        }
    ]
    # Projection preserves the requirement and hard-gate lists as separate
    # arrays; the merge into a single rendered list happens at commit and at
    # render time so evidence anchored to a gate id keeps a durable identity.
    assert [item.model_dump() for item in report.requirements] == [
        {
            "requirement_id": "R1",
            "importance": "must_have",
            "text": "Python",
            "evidence_keys": ["requirement-1-candidate-1"],
            "span_status": SpanStatus.VERIFIED,
            "established": True,
            "unmet": True,
            "dimension": "hard",
            "is_hard_gate": False,
            "gate_kind": None,
            "gate_quote": None,
            "gate_blocking": None,
        },
        {
            "requirement_id": "R2",
            "importance": "nice_to_have",
            "text": "Kubernetes",
            "evidence_keys": [],
            "span_status": SpanStatus.NEEDS_REVIEW,
            "established": True,
            "unmet": True,
            "dimension": "hard",
            "is_hard_gate": False,
            "gate_kind": None,
            "gate_quote": None,
            "gate_blocking": None,
        },
    ]
    assert [item.model_dump() for item in report.hard_gates] == [
        {
            "requirement_id": "gate-1",
            "importance": "must_have",
            "text": "Must be in Canada",
            "evidence_keys": ["hard-gate-1"],
            "span_status": SpanStatus.VERIFIED,
            # The rules read no residence from this posting, so the gate is unestablished.
            "established": False,
            "unmet": False,
            "dimension": "hard",
            "is_hard_gate": True,
            "gate_kind": "location",
            "gate_quote": "",
            "gate_blocking": True,
        },
        {
            "requirement_id": "gate-2",
            "importance": "nice_to_have",
            "text": "A degree is preferred",
            "evidence_keys": ["hard-gate-2"],
            "span_status": SpanStatus.NEEDS_REVIEW,
            "established": False,
            "unmet": False,
            "dimension": "hard",
            "is_hard_gate": True,
            "gate_kind": "degree",
            "gate_quote": "",
            "gate_blocking": False,
        },
    ]
    assert [item.question for item in report.reading] == [
        answer.question for _, answer in output.reading.named()
    ]
    assert [item.evidence_keys for item in report.reading] == list(reading_keys.values())
    assert [(item.question, item.answer, item.note, item.conflict) for item in report.reading] == [
        (
            answer.question,
            None if answer.value is None else str(answer.value),
            answer.note,
            answer.conflict,
        )
        for _, answer in output.reading.named()
    ]
    assert (report.coverage_present, report.coverage_total) == (1, 2)
    assert [item.model_dump() for item in report.checks] == [
        {
            "check": "candidate evidence anchors",
            "result": "pass",
            "detail": "Every stated score is linked to stored source evidence.",
        },
        {
            "check": "example-list notes",
            "result": "none flagged",
            "detail": "No match note holds a listed example, or the employer's own name or "
            "offering, against the record.",
        },
    ]
    assert report.length_budget == (
        "No derivative application document was generated during evaluation."
    )
    assert [
        (item.stage, item.model, item.input_tokens, item.output_tokens, item.amount)
        for item in report.costs
    ] == [
        ("jobspec", "model-a", 20, 8, None),
        ("match", "model-b", 30, 10, None),
    ]

    empty_spec = output.spec.model_copy(update={"requirements": (), "hard_gates": ()})
    fallback = project_fit_report(
        replace(output, spec=empty_spec),
        candidate_text="No keywords here.",
        requirement_keys={},
        hard_gate_evidence_keys=(),
        reading_keys=reading_keys,
        disagreements=[],
        fallback_evidence_key="posting-body",
    )

    assert [item.model_dump() for item in fallback.requirements] == [
        {
            "requirement_id": "posting",
            "importance": "responsibility",
            "text": "Review the stored posting body.",
            "evidence_keys": ["posting-body"],
            "span_status": SpanStatus.NEEDS_REVIEW,
            "established": True,
            "unmet": False,
            "dimension": "hard",
            "is_hard_gate": False,
            "gate_kind": None,
            "gate_quote": None,
            "gate_blocking": None,
        }
    ]
    assert fallback.hard_gates == []


@pytest.mark.parametrize(
    ("note", "result", "detail"),
    [
        pytest.param(
            "Streaming work transfers; Kafka is not established.",
            "advisory",
            'The note for "Operate streaming ingestion, such as Kafka or Kinesis" says '
            '"Kafka is not established"',
            id="example",
        ),
        pytest.param(
            "Batch work transfers; streaming is not established.",
            "none flagged",
            "No match note holds a listed example",
            id="ask",
        ),
    ],
)
def test_a_note_calling_a_listed_example_missing_is_an_advisory_check(
    note: str, result: str, detail: str
) -> None:
    output = _output()
    requirement = Requirement(
        id="R1",
        text="Operate streaming ingestion, such as Kafka or Kinesis",
        importance=Importance.MUST_HAVE,
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="R1",
                entry_ids=("b-1a2b3c4d",),
                classification=Classification.TRANSFERABLE,
                strength=Strength.MODERATE,
                note=note,
            ),
        )
    )

    report = project_fit_report(
        replace(
            output,
            spec=output.spec.model_copy(update={"requirements": (requirement,)}),
            evidence=evidence,
        ),
        candidate_text="Built batch pipelines.",
        requirement_keys={},
        hard_gate_evidence_keys=("hard-gate-1", "hard-gate-2"),
        reading_keys={name: [] for name, _ in output.reading.named()},
        disagreements=[],
        fallback_evidence_key="posting-body",
    )

    (example,) = [item for item in report.checks if item.check == "example-list notes"]
    assert example.result == result
    assert detail in example.detail


def test_each_requirement_keeps_its_own_dimension_and_gates_stay_hard() -> None:
    output = _output()
    python, _ = output.spec.requirements
    behavioral = Requirement(
        id="R2",
        text="Bias for shipping",
        importance=Importance.MUST_HAVE,
        dimension=Dimension.BEHAVIORAL,
    )
    spec = output.spec.model_copy(update={"requirements": (python, behavioral)})

    report = project_fit_report(
        replace(output, spec=spec),
        candidate_text="Built Python services.",
        requirement_keys={},
        hard_gate_evidence_keys=("hard-gate-1", "hard-gate-2"),
        reading_keys={name: [] for name, _ in output.reading.named()},
        disagreements=[],
        fallback_evidence_key="posting-body",
    )

    assert [(item.requirement_id, item.dimension) for item in report.requirements] == [
        ("R1", "hard"),
        ("R2", "behavioral"),
    ]
    assert [item.dimension for item in report.hard_gates] == ["hard", "hard"]


_GATE_POSTING = (
    "# Platform Engineer\n\n## Requirements\n"
    "- A bachelor's degree in computer science\n"
    "- Five years of experience with Python, required\n"
    "- Must reside in the state of hire\n"
    "- Must be available to work evenings and weekends\n"
    "- Active security clearance"
)


def _gate_output(kind: GateKind, text: str, classification: Classification) -> EvaluationOutput:
    """A posting stating one absolute gate, with a must-have restating part of it."""
    restated = Requirement(
        id="R1",
        text=f"{text} for this role",
        importance=Importance.MUST_HAVE,
        source_span=locate(text, _GATE_POSTING),
    )
    output = _output()
    spec = output.spec.model_copy(
        update={
            "requirements": (restated,),
            "hard_gates": (
                HardGate(
                    kind=kind,
                    text=text,
                    blocking=True,
                    source_span=locate(text, _GATE_POSTING),
                ),
            ),
        }
    )
    strength = Strength.NONE if classification is Classification.GAP else Strength.STRONG
    match = Match(
        requirement_id="R1",
        entry_ids=() if classification is Classification.GAP else ("b-1a2b3c4d",),
        classification=classification,
        strength=strength,
    )
    jd = JDText(text=_GATE_POSTING, origin="posting.txt", method="direct_text")
    return replace(output, spec=spec, evidence=EvidenceMap(matches=(match,)), jd=jd)


@pytest.mark.parametrize(
    ("kind", "text", "classification", "established", "unmet"),
    [
        ("degree", "A bachelor's degree in computer science", Classification.GAP, True, True),
        ("clearance", "Active security clearance", Classification.GAP, True, True),
        (
            "minimum_years",
            "Five years of experience with Python",
            Classification.GAP,
            True,
            True,
        ),
        ("degree", "A bachelor's degree in computer science", Classification.DIRECT, True, False),
        # The rules read no residence here, so the parameter gate stands unestablished.
        ("location", "Must reside in the state of hire", Classification.GAP, False, False),
        (
            "other",
            "Must be available to work evenings and weekends",
            Classification.GAP,
            True,
            False,
        ),
    ],
)
def test_a_gate_row_stores_the_verdict_the_record_gives_it(
    kind: GateKind, text: str, classification: Classification, established: bool, unmet: bool
) -> None:
    """A gate the record leaves unanswered reads as unmet; one it cannot answer reads as neither."""
    output = _gate_output(kind, text, classification)

    report = project_fit_report(
        output,
        candidate_text="Built Python services.",
        requirement_keys={},
        hard_gate_evidence_keys=("hard-gate-1",),
        reading_keys={name: [] for name, _ in output.reading.named()},
        disagreements=[],
        fallback_evidence_key="posting-body",
    )

    (gate,) = report.hard_gates
    assert (gate.gate_blocking, gate.established, gate.unmet) == (True, established, unmet)


@pytest.mark.parametrize("status", [OUTSIDE_DECLARED, "ineligible"])
def test_a_residence_gate_row_is_unmet_when_the_place_reading_rules_you_out(status: str) -> None:
    output = _gate_output("location", "Must reside in the state of hire", Classification.GAP)
    reading = replace(
        output.reading,
        place_fit=fit.Answer("Where you could work this", value=f"US: {status}"),
        residence_requirement=fit.Answer("Residence the posting requires", value="state of hire"),
    )

    report = project_fit_report(
        replace(output, reading=reading),
        candidate_text="Built Python services.",
        requirement_keys={},
        hard_gate_evidence_keys=("hard-gate-1",),
        reading_keys={name: [] for name, _ in output.reading.named()},
        disagreements=[],
        fallback_evidence_key="posting-body",
    )

    (gate,) = report.hard_gates
    assert (gate.established, gate.unmet) == (True, True)


def test_a_record_gate_nothing_restates_is_stored_as_not_established() -> None:
    output = _gate_output("clearance", "Active security clearance", Classification.GAP)
    unrelated = Requirement(id="R1", text="Kubernetes", importance=Importance.NICE_TO_HAVE)

    report = project_fit_report(
        replace(output, spec=output.spec.model_copy(update={"requirements": (unrelated,)})),
        candidate_text="Built Python services.",
        requirement_keys={},
        hard_gate_evidence_keys=("hard-gate-1",),
        reading_keys={name: [] for name, _ in output.reading.named()},
        disagreements=[],
        fallback_evidence_key="posting-body",
    )

    (gate,) = report.hard_gates
    assert (gate.established, gate.unmet) == (False, False)


def test_hard_gate_text_repeating_a_requirement_is_projected_as_one_row() -> None:
    """A gate whose text repeats a requirement folds into that row, not a second one."""
    posting = "# Platform Engineer\n\n## Requirements\n- Must be eligible to work in Canada"
    inventory = CvInventory(source=Path("master.md"), content_hash="12345678", name="Candidate")
    output = EvaluationOutput(
        spec=JobSpec(
            title="Platform Engineer",
            company="Example",
            seniority=None,
            location="Canada",
            requirements=(
                Requirement(
                    id="R1",
                    text="Must be eligible to work in Canada",
                    importance=Importance.MUST_HAVE,
                    source_span=locate("Must be eligible to work in Canada", posting),
                ),
            ),
            keywords=(),
            hard_gates=(
                HardGate(
                    kind="work_authorization",
                    text="Must be eligible to work in Canada",
                    blocking=True,
                    source_span=locate("Must be eligible to work in Canada", posting),
                ),
            ),
            origin="posting.txt",
        ),
        evidence=EvidenceMap(),
        inventory=inventory,
        ledger=Ledger(),
        jd=JDText(text=posting, origin="posting.txt", method="direct_text"),
        reading=fit.read(posting, inventory, today=date(2026, 8, 11)),
        outcome="pursue",
        reasons=("The posting was evaluated.",),
        prompt_fingerprint="prompt-suite",
    )

    report = project_fit_report(
        output,
        candidate_text="Eligible to work in Canada.",
        requirement_keys={"R1": ["requirement-1-candidate-1"]},
        hard_gate_evidence_keys=("hard-gate-1",),
        reading_keys={name: [] for name, _ in output.reading.named()},
        disagreements=[],
        fallback_evidence_key="posting-body",
    )

    # The projection keeps both lists raw; the requirement and the gate that
    # repeats it fold together only at commit and at render time.
    assert len(report.requirements) == 1
    assert report.requirements[0].requirement_id == "R1"
    assert report.requirements[0].is_hard_gate is False
    assert report.requirements[0].evidence_keys == ["requirement-1-candidate-1"]
    assert len(report.hard_gates) == 1
    assert report.hard_gates[0].requirement_id == "gate-1"
    assert report.hard_gates[0].text == "Must be eligible to work in Canada"
    assert report.hard_gates[0].gate_kind == "work_authorization"
    assert report.hard_gates[0].gate_blocking is True
    assert report.hard_gates[0].evidence_keys == ["hard-gate-1"]
