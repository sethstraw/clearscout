"""Small-batch screening: several postings at once, and none of them mixed up.

The two guarantees worth more than the rest: no prompt sent while screening one
posting carries text from another, and a cached answer for one posting does not
answer another. Everything else here is arithmetic over screening results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tailorcv.application.config import ModelChoice, Settings
from tailorcv.application.tailoring import batch
from tailorcv.application.tailoring.batch import Request, Verdict
from tailorcv.application.tailoring.batch.analysis import (
    _common_terms,
    _Entry,
    _same_requirement,
    _terms,
    recommend,
)
from tailorcv.application.tailoring.stages.jobspec import (
    Dimension,
    HardGate,
    Importance,
    JobSpec,
    Requirement,
    StoredPostingName,
)
from tailorcv.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from tailorcv.application.tailoring.stages.spans import Span
from tailorcv.domain import EvidenceStatus, GapClass
from tailorcv.domain.records import SpanStatus
from tailorcv.domain.requirements import GateKind
from tailorcv.domain.taxonomy import extended
from tailorcv.infrastructure.providers.base import CallRecord, json_schema
from tailorcv.infrastructure.providers.cache import cache_key
from tailorcv.infrastructure.providers.plan import RequestPlan
from tailorcv.shared.costs import Usage
from tailorcv.shared.errors import ConfigError
from tests.support.schema import literals
from tests.support.suite import SUITE_KEY_VARIABLE, SUITE_PROVIDER

# Two postings for related work that ask for one thing in words with almost
# nothing in common, which is the case shared-requirement detection exists for.
GLOBEX = (
    "Data Platform Engineer at Globex. Fusion Ledger is our billing system. "
    "Requirements: experience running Kubernetes in production, "
    "and Kafka schema governance across teams. "
    "Remote within the United States. "
) * 6
INITECH = (
    "Streaming Engineer at Initech. Pelican Router is our routing tier. "
    "Requirements: hands-on with Kubernetes clusters, "
    "and Airflow scheduling for nightly loads. "
    "Active security clearance required. "
    "Hybrid, two days per week in the office. "
) * 6

# One phrase each, which appears in that posting and nowhere else. Used to prove
# a prompt built for one posting carries nothing from the other.
MARKERS = {"globex": "Fusion Ledger", "initech": "Pelican Router"}

SPECS: dict[str, dict[str, Any]] = {
    "globex": {
        "title": "Data Platform Engineer",
        "company": "Globex",
        "seniority": None,
        "location": "Remote, United States",
        "requirements": [
            {"text": "experience running Kubernetes in production", "importance": "must_have"},
            {"text": "Kafka schema governance across teams", "importance": "must_have"},
        ],
        "keywords": ["Kubernetes", "Kafka"],
        "hard_gates": [
            {
                "kind": "location",
                "text": "Remote within the US",
                "blocking": False,
                "quote_or_span": "Remote within the United States",
            }
        ],
    },
    "initech": {
        "title": "Streaming Engineer",
        "company": "Initech",
        "seniority": None,
        "location": "Hybrid",
        "requirements": [
            {"text": "hands-on with Kubernetes clusters", "importance": "must_have"},
            {"text": "Airflow scheduling for nightly loads", "importance": "nice_to_have"},
        ],
        "keywords": ["Kubernetes", "Airflow"],
        "hard_gates": [
            {"kind": "location", "text": "Two days per week in the office", "blocking": True},
            {
                "kind": "clearance",
                "text": "Active security clearance required",
                "blocking": True,
            },
        ],
    },
}


class BatchProvider:
    """A provider that answers per posting, and records every request verbatim.

    These tests are about which posting a request was built from, which a
    provider keyed only on the response schema cannot express.
    """

    name = SUITE_PROVIDER

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, RequestPlan, str]] = []

    def complete(
        self, plan: RequestPlan, schema: type[BaseModel], choice: ModelChoice
    ) -> tuple[BaseModel, CallRecord]:
        whole = f"{plan.instructions.text}\n{plan.task.text}\n{plan.cached_prefix}"
        posting = next(
            (key for key, marker in MARKERS.items() if marker in whole),
            "",
        )
        stage = "jobspec" if "JobSpec" in schema.__name__ else "match"
        if not posting and stage == "match":
            # Matching is sent the requirements rather than the posting, so it
            # is identified by the wording only one of the two states.
            posting = "globex" if "schema governance" in whole else "initech"
        key = cache_key(
            provider=self.name.value,
            stage=stage,
            max_tokens=plan.max_tokens,
            model=f"{choice.model}:{choice.effort.value if choice.effort else 'default'}",
            system=plan.instructions.text,
            user=plan.task.text,
            schema=json_schema(schema),
            extra=plan.cached_prefix,
        )
        self.calls.append((stage, posting, plan, key))
        payload = SPECS[posting] if stage == "jobspec" else self._match(schema, posting)
        return schema.model_validate(payload), CallRecord(
            usage=Usage(input_tokens=10, output_tokens=5), model=choice.model
        )

    def _match(self, schema: type[BaseModel], posting: str) -> dict[str, Any]:
        """Answer matching: the Kubernetes requirement is a gap in both postings."""
        entry_ids = literals(schema, "entry_ids")
        bullets = [entry_id for entry_id in entry_ids if entry_id.startswith("b-")] or entry_ids
        return {
            "matches": [
                {
                    "requirement_id": "r01",
                    "entry_ids": [],
                    "classification": "GAP",
                    "strength": "none",
                    "note": "nothing in the record answers it",
                },
                {
                    "requirement_id": "r02",
                    "entry_ids": [bullets[0]],
                    "classification": "DIRECT",
                    "strength": "strong",
                    "note": "",
                },
            ]
        }

    def check_credentials(self) -> None:
        """Do nothing: no credential exists here and none may be needed."""

    def available_models(self) -> frozenset[str] | None:
        """Return nothing, which is what a provider that was not asked would say."""
        return None

    def prompts_for(self, posting: str) -> list[str]:
        """Return every character sent while screening one posting."""
        return [
            f"{plan.instructions.text}\n{plan.task.text}\n{plan.cached_prefix}"
            for _, key, plan, _ in self.calls
            if key == posting
        ]

    def keys_for(self, posting: str) -> set[str]:
        """Return the cache keys written while screening one posting."""
        return {key for _, name, _, key in self.calls if name == posting}


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BatchProvider:
    """Point screening at a scripted provider and a cache under the temp path."""
    built = BatchProvider()
    monkeypatch.setattr(
        "tailorcv.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: built,
    )
    monkeypatch.setattr(
        "tailorcv.infrastructure.providers.cache.user_cache_dir",
        lambda name: str(tmp_path / "cache"),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    return built


@pytest.fixture
def postings(tmp_path: Path) -> list[Request]:
    """Write the two postings to disk and return them as batch requests."""
    made: list[Request] = []
    for name, text in (("globex", GLOBEX), ("initech", INITECH)):
        path = tmp_path / f"{name}.txt"
        path.write_text(text, encoding="utf-8")
        made.append(Request(source=str(path)))
    return made


def run(master_cv: Path, tmp_path: Path, requests: list[Request], **kwargs: Any) -> Any:
    """Screen a batch against the synthetic master CV."""
    settings = Settings(cv=master_cv, output_dir=tmp_path / "runs")
    return batch.screen_batch(requests, settings, **kwargs)


def test_every_posting_gets_its_own_specification_and_evidence_map(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    assert len(result.screenings) == 2
    keys = [item.key for item in result.screenings]
    assert len(set(keys)) == 2
    assert {item.spec.company for item in result.screenings} == {"Globex", "Initech"}
    # Separate objects, not one merged pair of tables.
    first, second = result.screenings
    assert first.spec is not second.spec
    assert first.evidence is not second.evidence
    assert first.posting_id != second.posting_id


def test_no_prompt_carries_text_from_a_second_posting(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """The guarantee that makes "never combine postings" mean something."""
    run(master_cv, tmp_path, postings, cache_enabled=False)
    assert provider.calls
    for name, other in (("globex", "initech"), ("initech", "globex")):
        for sent in provider.prompts_for(name):
            assert MARKERS[other] not in sent


def test_a_batch_drafts_nothing(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    run(master_cv, tmp_path, postings, cache_enabled=False)
    assert {stage for stage, _, _, _ in provider.calls} == {"jobspec", "match"}


def test_a_batch_keeps_the_spend_each_screening_made(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """A comparison that discards its ledger reports nothing spent, however much it cost."""
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)

    assert len(result.screenings) == 2
    for item in result.screenings:
        assert {entry.stage for entry in item.ledger.entries} == {"jobspec", "match"}


def test_a_cache_entry_for_one_posting_cannot_answer_another(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """With the cache on, every posting pays for its own extraction and its own match."""
    run(master_cv, tmp_path, postings, cache_enabled=True)
    assert len(provider.calls) == 4
    assert provider.keys_for("globex").isdisjoint(provider.keys_for("initech"))


def test_screening_the_same_batch_again_asks_nothing(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """The other half of the cache claim: reuse works, per posting."""
    run(master_cv, tmp_path, postings, cache_enabled=True)
    provider.calls.clear()
    run(master_cv, tmp_path, postings, cache_enabled=True)
    assert provider.calls == []


def test_a_requirement_two_postings_state_in_different_words_is_found(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """One requirement, one shared word, and eight words that are not shared.

    "experience running Kubernetes in production" and "hands-on with Kubernetes
    clusters" are the case this exists for.
    """
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    assert len(result.shared) == 1
    cluster = result.shared[0]
    assert cluster.leverage == 2
    assert cluster.joined_by == ("kubernetes",)
    assert {text for _, text in cluster.wordings} == {
        "experience running Kubernetes in production",
        "hands-on with Kubernetes clusters",
    }


def test_a_word_the_whole_batch_uses_joins_nothing() -> None:
    """The rule that stops "experience" putting every posting in one cluster."""
    entries = [
        _Entry(
            posting=f"p{index}",
            requirement_id="r01",
            text=text,
            must_have=True,
            blocking=False,
            terms=_terms(text),
        )
        for index, text in enumerate(
            (
                "strong experience with widgets",
                "strong experience with sprockets",
                "strong experience with flanges",
                "strong experience with cogs",
            )
        )
    ]
    common = _common_terms(entries)
    assert "experience" in common
    assert "strong" in common
    assert "widgets" not in common
    # Three words in four are shared, and these are two different requirements.
    assert _same_requirement(entries[0], entries[1], common, extended()) is None
    # The same sentence twice is one requirement, however common its words are.
    assert _same_requirement(entries[0], entries[0], common, extended()) == ("the same wording",)


def test_gaps_are_ranked_by_how_many_postings_one_closed_gap_would_help(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    assert [cluster.leverage for cluster in result.opportunities] == [2]
    top = result.opportunities[0]
    assert top.subject == "kubernetes"
    assert len(top.gap_ids) == 2
    assert len(top.must_have_in) == 2


def test_the_shared_discovery_queue_is_where_the_two_features_meet(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    lines = batch.queue(result)
    assert [line.subject for line in lines] == ["kubernetes"]
    assert len(lines[0].gap_ids) == 2
    assert len(lines[0].postings) == 2
    # And a line of questioning is ready to ask, without a model.
    question = lines[0].next_question()
    assert question is not None
    assert "kubernetes" in question.text


def test_the_recommendation_carries_reasons_and_no_number_that_reads_as_odds(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, inventory = run(master_cv, tmp_path, postings, cache_enabled=False)
    report = batch.markdown(result, inventory)

    for recommendation in result.recommendations.values():
        assert recommendation.verdict in set(Verdict)
        assert recommendation.reasons
    # No percentage, no grade, and the only time the word appears is the line
    # saying there is not one.
    assert "%" not in report
    assert "out of 10" not in report
    assert report.casefold().count("score") == 1
    assert "Nothing here is a score" in report


def test_an_absolute_condition_this_run_never_paired_is_a_cautious_pursue(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """Initech states a certification absolutely and nothing was paired to it.

    Postings that list alternative qualifying routes state each as its own gate,
    so an unpaired gate is routine and does not mean the record fails it. It is
    reported either way; what the run answers of the posting decides the verdict.

    Initech also states an office requirement. That one is settled by the search
    parameters rather than by the record, so it is reported separately and never
    counted as something the record failed to answer.
    """
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    initech = next(item for item in result.screenings if item.spec.company == "Initech")
    recommendation = result.recommendations[initech.key]
    assert recommendation.verdict is not Verdict.PURSUE
    assert any("paired nothing to it" in reason for reason in recommendation.reasons)
    assert any(
        "your search parameters answer it rather than the record" in reason
        for reason in recommendation.reasons
    )
    assert not any(
        "paired nothing to it" in reason and "office" in reason for reason in recommendation.reasons
    )


def test_an_essential_requirement_the_record_cannot_answer_is_never_a_pursue(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """An unmet essential requirement is reported and stops a pursue.

    It does not force the middle verdict on its own: on a posting stating dozens
    of requirements at least one almost always goes unmet.
    """
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    globex = next(item for item in result.screenings if item.spec.company == "Globex")
    recommendation = result.recommendations[globex.key]
    assert recommendation.verdict is not Verdict.PURSUE
    assert "stated as essential" in recommendation.reasons[0]


def test_a_posting_with_nothing_against_it_is_a_pursue(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    globex = next(item for item in result.screenings if item.spec.company == "Globex")
    answered = globex.evidence.model_copy(
        update={
            "matches": tuple(
                match.model_copy(
                    update={
                        "classification": Classification.DIRECT,
                        "strength": "strong",
                        "entry_ids": (globex.evidence.matches[1].entry_ids[0],),
                    }
                )
                for match in globex.evidence.matches
            )
        }
    )
    recommendation = batch.recommend(globex.spec, answered)
    assert recommendation.verdict is Verdict.PURSUE
    assert "answers 100 per cent of what the posting weights" in recommendation.reasons[0]


def test_the_fit_explanation_counts_rather_than_grades(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False)
    globex = next(item for item in result.screenings if item.spec.company == "Globex")
    sentence = batch.fit(globex.spec, globex.evidence)
    assert sentence.startswith("Of 2 extracted requirements:")
    assert "1 answered by cited evidence" in sentence
    assert "1 not answered" in sentence


def test_the_report_answers_every_question_a_batch_is_read_for(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, inventory = run(master_cv, tmp_path, postings, cache_enabled=False)
    written = batch.write(tmp_path, result, inventory)
    report = written.read_text(encoding="utf-8")

    assert written.name == batch.REPORT
    assert [item.report for item in result.screenings] == [None, None]
    for expected in (
        "Data Platform Engineer at Globex",
        "Streaming Engineer at Initech",
        "Deal-breakers the posting states:",
        "Strongest evidence:",
        "Material gaps:",
        "Shared with the rest of the batch:",
        "High-leverage discovery for this posting:",
        "What these postings ask for in common",
        "Shared discovery queue",
        "tailorcv discover --queue batch.json",
    ):
        assert expected in report


def test_stored_titles_and_employers_name_each_posting_in_the_report_and_record(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    stored = [
        StoredPostingName(title="Platform Product Lead", employer="Example Logistics"),
        StoredPostingName(title="Streaming Platform Lead", employer="Initech Labs"),
    ]
    named = [
        Request(source=item.source, stored_name=name)
        for item, name in zip(postings, stored, strict=True)
    ]
    result, inventory = run(master_cv, tmp_path, named, cache_enabled=False)
    batch.write(tmp_path, result, inventory)

    report = (tmp_path / batch.REPORT).read_text(encoding="utf-8")
    assert "## Platform Product Lead at Example Logistics" in report
    assert "## Streaming Platform Lead at Initech Labs" in report
    assert "Data Platform Engineer" not in report
    record = json.loads((tmp_path / batch.RECORD).read_text(encoding="utf-8"))
    assert sorted((item["role"], item["company"]) for item in record["postings"]) == [
        ("Platform Product Lead", "Example Logistics"),
        ("Streaming Platform Lead", "Initech Labs"),
    ]


def test_the_record_carries_the_queue_a_discovery_session_reads_back(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    result, inventory = run(master_cv, tmp_path, postings, cache_enabled=False)
    batch.write(tmp_path, result, inventory)
    record = tmp_path / batch.RECORD

    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["schema"] == batch.SCHEMA
    assert [item["key"] for item in payload["postings"]] == [item.key for item in result.ordered()]
    lines = batch.lines_from(record)
    assert [line.subject for line in lines] == ["kubernetes"]


def test_a_record_from_another_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "batch.json"
    path.write_text(json.dumps({"schema": 99, "queue": []}), encoding="utf-8")
    with pytest.raises(ConfigError, match="not a batch record"):
        batch.lines_from(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        batch.lines_from(path)
    with pytest.raises(ConfigError, match="Cannot read"):
        batch.lines_from(tmp_path / "gone.json")


def test_each_posting_keeps_its_own_screening_report(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """A batch report is a comparison; a screening report is one posting's own."""
    result, _ = run(master_cv, tmp_path, postings, cache_enabled=False, out=tmp_path / "slate")
    written = [item.report for item in result.screenings]
    assert all(path is not None and path.is_file() for path in written)
    assert len({path.parent for path in written if path is not None}) == 2


def test_priorities_and_notes_are_carried_and_order_the_reading(
    master_cv: Path, tmp_path: Path, postings: list[Request], provider: BatchProvider
) -> None:
    """The person's own ordering outranks anything the program derived."""
    prioritised = [
        postings[0],
        Request(source=postings[1].source, priority=3, note="referral from a former colleague"),
    ]
    result, inventory = run(master_cv, tmp_path, prioritised, cache_enabled=False)
    assert result.ordered()[0].spec.company == "Initech"
    report = batch.markdown(result, inventory)
    assert "Your priority: 3" in report
    assert "referral from a former colleague" in report


def test_a_batch_file_supplies_the_sources_priorities_and_notes(tmp_path: Path) -> None:
    path = tmp_path / "batch.toml"
    path.write_text(
        '[[posting]]\nsource = "a.txt"\n\n'
        '[[posting]]\nsource = "b.txt"\npriority = 2\nnote = "closes soon"\n',
        encoding="utf-8",
    )
    requests = batch.requests_from(path)
    assert [item.source for item in requests] == ["a.txt", "b.txt"]
    assert requests[1].priority == 2
    assert requests[1].note == "closes soon"


def test_a_batch_file_that_is_not_one_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "batch.toml"
    path.write_text("[[posting]]\npriority = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="posting 1 has no source"):
        batch.requests_from(path)
    path.write_text("nothing = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="lists no postings"):
        batch.requests_from(path)
    path.write_text("[[posting\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        batch.requests_from(path)
    with pytest.raises(ConfigError, match="Cannot read the batch file"):
        batch.requests_from(tmp_path / "gone.toml")


def test_a_batch_of_one_or_of_too_many_is_refused(master_cv: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="tailorcv screen"):
        run(master_cv, tmp_path, [Request(source="one.txt")])
    too_many = [Request(source=f"{index}.txt") for index in range(batch.MAX_POSTINGS + 1)]
    with pytest.raises(ConfigError, match="more than a batch"):
        run(master_cv, tmp_path, too_many)


def test_two_postings_with_the_same_identity_still_get_separate_keys(
    master_cv: Path, tmp_path: Path, provider: BatchProvider
) -> None:
    """A key names a posting inside one batch, so it cannot repeat."""
    path = tmp_path / "posting.txt"
    path.write_text(GLOBEX, encoding="utf-8")
    listed_twice = [Request(source=str(path)), Request(source=str(path))]

    result, _ = run(master_cv, tmp_path, listed_twice, cache_enabled=False)

    first, second = (item.key for item in result.screenings)
    assert second == f"{first}-2"


def test_one_unestablished_gate_alone_does_not_block_a_pursue() -> None:
    """One unpaired gate is tolerated. A second one, or no cited evidence, is not."""
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        HardGate,
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )
    from tailorcv.application.tailoring.stages.spans import Span
    from tailorcv.domain.records import SpanStatus

    def _gate(text: str) -> HardGate:
        return HardGate(
            kind="clearance",
            text=text,
            blocking=True,
            quote_or_span=text,
            source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(text)),
        )

    def _spec(*gates: HardGate) -> JobSpec:
        return JobSpec(
            title="Analyst",
            company="Initrode",
            seniority=None,
            location="Remote",
            keywords=(),
            origin="",
            requirements=(
                Requirement(
                    id="r01",
                    text="Five years of Python.",
                    importance=Importance.NICE_TO_HAVE,
                ),
            ),
            hard_gates=gates,
        )

    answered = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            ),
        )
    )
    one = _gate("You must hold an active security clearance.")
    two = _gate("You must be willing to relocate to Springfield.")

    assert recommend(_spec(one), answered).verdict is Verdict.PURSUE
    assert recommend(_spec(one, two), answered).verdict is Verdict.CAUTIOUS
    assert recommend(_spec(one), EvidenceMap()).verdict is Verdict.CAUTIOUS


def test_a_posting_that_states_no_must_have_is_not_a_pursue() -> None:
    """A body cut short of its requirements cannot show the bar was cleared."""
    spec = JobSpec(
        title="Analyst",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=tuple(
            Requirement(id=f"r0{n}", text=text, importance=Importance.NICE_TO_HAVE)
            for n, text in enumerate(("Python.", "SQL.", "Healthcare data."), start=1)
        ),
        hard_gates=(),
    )
    answered = EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=requirement.id,
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            )
            for requirement in spec.requirements
        )
    )

    result = recommend(spec, answered)

    assert result.verdict is Verdict.CAUTIOUS
    assert any("no requirement is marked required" in reason for reason in result.reasons)


def test_a_blocking_gate_the_record_fails_is_still_a_skip() -> None:
    """An unpaired gate is unestablished; one paired to a gap is a real failure.

    Built directly rather than through a run, because the difference is which
    classification the gate carries and nothing else.
    """
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        HardGate,
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )
    from tailorcv.application.tailoring.stages.spans import Span
    from tailorcv.domain.records import SpanStatus

    text = "You must hold an active security clearance."
    spec = JobSpec(
        title="Analyst",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(Requirement(id="r01", text=text, importance=Importance.MUST_HAVE),),
        hard_gates=(
            HardGate(
                kind="clearance",
                text=text,
                blocking=True,
                quote_or_span=text,
                source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(text)),
            ),
        ),
    )
    failed = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                classification=Classification.GAP,
                strength=Strength.NONE,
            ),
        )
    )

    assert recommend(spec, failed).verdict is Verdict.SKIP
    assert recommend(spec, EvidenceMap()).verdict is Verdict.CAUTIOUS


def test_a_compound_degree_gate_answered_in_part_does_not_force_a_skip() -> None:
    """A degree gate bundling a held bachelor's and an unheld master's abstains.

    The master's clause is a word-subset of the gate quote, so pairing on overlap
    alone would let it define the whole gate and hard-skip the fit.
    """
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        HardGate,
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
        answered_gates,
    )
    from tailorcv.application.tailoring.stages.spans import Span
    from tailorcv.domain.records import SpanStatus

    gate_text = (
        "Bachelor's degree in Widget Engineering or a related field and a "
        "Master's degree in Sprocket Studies."
    )
    requirements = (
        Requirement(
            id="r01",
            text="Bachelor's degree in Widget Engineering, Gadgetry, or a related field.",
            importance=Importance.MUST_HAVE,
        ),
        Requirement(
            id="r02",
            text="Master's degree in Sprocket Studies.",
            importance=Importance.MUST_HAVE,
        ),
        Requirement(
            id="r03", text="Experience with data pipelines.", importance=Importance.MUST_HAVE
        ),
        Requirement(
            id="r04", text="Proficiency in SQL and Python.", importance=Importance.MUST_HAVE
        ),
        Requirement(
            id="r05", text="Experience with cloud platforms.", importance=Importance.MUST_HAVE
        ),
    )
    spec = JobSpec(
        title="Staff Widget Data Architect",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=requirements,
        hard_gates=(
            HardGate(
                kind="degree",
                text=gate_text,
                blocking=True,
                quote_or_span=gate_text,
                source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(gate_text)),
            ),
        ),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            ),
            Match(requirement_id="r02", classification=Classification.GAP, strength=Strength.NONE),
            *(
                Match(
                    requirement_id=rid,
                    classification=Classification.DIRECT,
                    strength=Strength.STRONG,
                    entry_ids=("e1",),
                )
                for rid in ("r03", "r04", "r05")
            ),
        )
    )

    (gate,) = answered_gates(spec, evidence)
    assert gate.classification is None
    assert gate.needs_a_human

    recommendation = recommend(spec, evidence)
    assert recommendation.verdict is Verdict.CAUTIOUS
    assert not any(
        "nothing in the record answers it" in reason for reason in recommendation.reasons
    )
    assert any("stated as essential" in reason for reason in recommendation.reasons)


def test_a_partial_blocking_gate_settles_nothing_so_a_strong_fit_is_cautious() -> None:
    """A lone partial blocking gate blocks a pursue with no essential gap in play.

    The gapped clause is a responsibility, so the essential cap that would catch
    a must-have never fires; the partial gate itself is what settles nothing.
    """
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        HardGate,
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
        answered_gates,
    )
    from tailorcv.application.tailoring.stages.spans import Span
    from tailorcv.domain.records import SpanStatus

    gate_text = "alpha beta gamma"
    spec = JobSpec(
        title="Analyst",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(
            Requirement(id="r01", text="alpha beta gamma one", importance=Importance.MUST_HAVE),
            Requirement(
                id="r02", text="alpha beta gamma two", importance=Importance.RESPONSIBILITY
            ),
            Requirement(id="r03", text="delta epsilon zeta", importance=Importance.MUST_HAVE),
            Requirement(id="r04", text="eta theta iota", importance=Importance.MUST_HAVE),
        ),
        hard_gates=(
            HardGate(
                kind="clearance",
                text=gate_text,
                blocking=True,
                quote_or_span=gate_text,
                source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(gate_text)),
            ),
        ),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            ),
            Match(requirement_id="r02", classification=Classification.GAP, strength=Strength.NONE),
            *(
                Match(
                    requirement_id=rid,
                    classification=Classification.DIRECT,
                    strength=Strength.STRONG,
                    entry_ids=("e1",),
                )
                for rid in ("r03", "r04")
            ),
        )
    )

    (gate,) = answered_gates(spec, evidence)
    assert gate.partial

    recommendation = recommend(spec, evidence)
    assert recommendation.verdict is Verdict.CAUTIOUS
    assert any("answers it only in part" in reason for reason in recommendation.reasons)


def test_an_excluded_core_occupation_deprioritizes_a_strong_fit() -> None:
    """A boundary matching the grounded occupation label skips even a pursue-level fit.

    The rule reads only the span-verified label, so an unverified label is inert.
    """
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        CoreOccupation,
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )
    from tailorcv.application.tailoring.stages.spans import Span
    from tailorcv.domain.records import SpanStatus

    label = "platform engineering"

    def _spec(occupation: CoreOccupation | None) -> JobSpec:
        return JobSpec(
            title="Engineer",
            company="Initrode",
            seniority=None,
            location="Remote",
            keywords=(),
            origin="",
            requirements=(
                Requirement(
                    id="r01", text="Build data pipelines.", importance=Importance.MUST_HAVE
                ),
                Requirement(
                    id="r02", text="Operate cloud services.", importance=Importance.MUST_HAVE
                ),
            ),
            hard_gates=(),
            core_occupation=occupation,
        )

    answered = EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=rid,
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            )
            for rid in ("r01", "r02")
        )
    )
    verified = CoreOccupation(
        text=label,
        quote_or_span=label,
        source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(label)),
    )
    unplaced = CoreOccupation(text=label, quote_or_span=label)

    excluded = recommend(_spec(verified), answered, boundaries=(label,))
    assert excluded.verdict is Verdict.SKIP
    assert any("rules out" in reason for reason in excluded.reasons)

    assert recommend(_spec(verified), answered).verdict is Verdict.PURSUE
    assert recommend(_spec(unplaced), answered, boundaries=(label,)).verdict is Verdict.PURSUE


@pytest.mark.parametrize(
    ("occupation", "boundary"),
    [
        ("product management", "product management or program management"),
        ("program management", "product management, or program management"),
        (
            "generic commercial product management",
            "people management of product managers; generic commercial product management",
        ),
    ],
)
def test_a_boundary_listing_alternatives_excludes_an_occupation_naming_one_of_them(
    occupation: str, boundary: str
) -> None:
    """Each alternative a boundary lists is a whole exclusion of its own."""
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        CoreOccupation,
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )
    from tailorcv.application.tailoring.stages.spans import Span
    from tailorcv.domain.records import SpanStatus

    spec = JobSpec(
        title="Manager",
        company="Initech",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(
            Requirement(id="r01", text="Own the roadmap.", importance=Importance.MUST_HAVE),
            Requirement(id="r02", text="Run launches.", importance=Importance.MUST_HAVE),
        ),
        hard_gates=(),
        core_occupation=CoreOccupation(
            text=occupation,
            quote_or_span=occupation,
            source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(occupation)),
        ),
    )
    evidence = EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=identifier,
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            )
            for identifier in ("r01", "r02")
        )
    )

    assert recommend(spec, evidence, boundaries=(boundary,)).verdict is Verdict.SKIP


@pytest.mark.parametrize(
    ("occupation", "boundary"),
    [
        ("product management", "people management of product managers"),
        (
            "product management",
            "generic commercial product management without a clinical, scientific, "
            "platform, or data qualifier",
        ),
        (
            "software platform engineering",
            "conventional software-platform, infrastructure, DevOps, AI/ML, or generic "
            "data engineering as the core occupation",
        ),
        (
            "SAS product management",
            "conventional CRO clinical data management (CDISC, SAS, EDC operations)",
        ),
    ],
)
def test_a_boundary_abstains_when_the_occupation_omits_its_qualifying_terms(
    occupation: str, boundary: str
) -> None:
    """A short occupation label cannot prove a qualified free-form exclusion."""
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        CoreOccupation,
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )
    from tailorcv.application.tailoring.stages.spans import Span
    from tailorcv.domain.records import SpanStatus

    spec = JobSpec(
        title="Engineer",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(
            Requirement(id="r01", text="Build data pipelines.", importance=Importance.MUST_HAVE),
            Requirement(id="r02", text="Operate cloud services.", importance=Importance.MUST_HAVE),
        ),
        hard_gates=(),
        core_occupation=CoreOccupation(
            text=occupation,
            quote_or_span=occupation,
            source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(occupation)),
        ),
    )
    evidence = EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=identifier,
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            )
            for identifier in ("r01", "r02")
        )
    )

    assert recommend(spec, evidence, boundaries=(boundary,)).verdict is Verdict.PURSUE


def test_a_rung_above_the_seniority_band_caps_a_strong_fit_at_cautious() -> None:
    """A posting above the band ceiling holds a pursue at cautious, and only above it.

    The check reads the rung from the title first and the seniority field only when
    the title names none, and abstains without a ceiling or when the posting names no
    rung, so a cap fires only on an altitude the search bounded and the posting states.
    """
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )
    from tailorcv.domain.search_parameters.roles import Seniority

    def _spec(title: str, seniority: str | None) -> JobSpec:
        return JobSpec(
            title=title,
            company="Initrode",
            seniority=seniority,
            location="Remote",
            keywords=(),
            origin="",
            requirements=(
                Requirement(
                    id="r01", text="Build data pipelines.", importance=Importance.MUST_HAVE
                ),
                Requirement(
                    id="r02", text="Operate cloud services.", importance=Importance.MUST_HAVE
                ),
            ),
            hard_gates=(),
        )

    answered = EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=rid,
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            )
            for rid in ("r01", "r02")
        )
    )
    band = Seniority(minimum="mid", maximum="senior")

    capped = recommend(_spec("Director of Data Products", None), answered, seniority=band)
    assert capped.verdict is Verdict.CAUTIOUS
    assert any("above the seniority band" in reason for reason in capped.reasons)

    # The rung is read from the seniority field when the title names none, and the
    # reason cites that field rather than the title it did not come from.
    from_field = recommend(_spec("Product Manager", "Director"), answered, seniority=band)
    assert from_field.verdict is Verdict.CAUTIOUS
    assert any(
        "Director" in reason and "Product Manager" not in reason for reason in from_field.reasons
    )

    # A title naming a rung outranks a lower rung in the seniority field.
    titled = recommend(_spec("Director, Analytics", "senior level"), answered, seniority=band)
    assert titled.verdict is Verdict.CAUTIOUS
    assert any("Director, Analytics" in reason for reason in titled.reasons)

    # No band, an in-band rung, and a posting naming no rung each leave the pursue.
    assert recommend(_spec("Director of Data Products", None), answered).verdict is Verdict.PURSUE
    inside = recommend(_spec("Senior Product Manager", None), answered, seniority=band)
    assert inside.verdict is Verdict.PURSUE
    unstated = recommend(_spec("Product Manager", None), answered, seniority=band)
    assert unstated.verdict is Verdict.PURSUE


def test_an_unmet_essential_caps_a_strong_fit_at_cautious_but_not_a_weak_one() -> None:
    """A must-have the record cannot answer stops a pursue without lifting a skip.

    The cap is a ceiling: a strong-fit posting with one unmet essential drops
    from pursue to investigate, while a genuinely weak fit with the same unmet
    essential still deprioritizes rather than being surfaced for review.
    """
    from tailorcv.application.tailoring.batch.analysis import recommend
    from tailorcv.application.tailoring.stages.jobspec import (
        Importance,
        JobSpec,
        Requirement,
    )
    from tailorcv.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )

    def _spec(*ids: str) -> JobSpec:
        return JobSpec(
            title="Analyst",
            company="Initrode",
            seniority=None,
            location="Remote",
            keywords=(),
            origin="",
            requirements=tuple(
                Requirement(id=rid, text=f"Requirement {rid}.", importance=Importance.MUST_HAVE)
                for rid in ids
            ),
            hard_gates=(),
        )

    def _answered(*direct: str, gap: str) -> EvidenceMap:
        matches = [
            Match(
                requirement_id=rid,
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            )
            for rid in direct
        ]
        matches.append(
            Match(requirement_id=gap, classification=Classification.GAP, strength=Strength.NONE)
        )
        return EvidenceMap(matches=tuple(matches))

    # Three answered must-haves against one gap is fit 75, a would-be pursue.
    strong = recommend(_spec("r1", "r2", "r3", "r4"), _answered("r1", "r2", "r3", gap="r4"))
    assert strong.verdict is Verdict.CAUTIOUS
    assert any("stated as essential" in reason for reason in strong.reasons)

    # One answered must-have against one gap is fit 50, a genuine skip.
    weak = recommend(_spec("r1", "r2"), _answered("r1", gap="r2"))
    assert weak.verdict is Verdict.SKIP


def _graded_posting(
    *rows: tuple[str, Importance, Classification, Strength],
    behavioral: frozenset[str] = frozenset(),
) -> tuple[JobSpec, EvidenceMap]:
    """A posting whose requirements are each answered at the stated grade."""
    spec = JobSpec(
        title="Data Engineer",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=tuple(
            Requirement(
                id=rid,
                text=f"Requirement {rid}.",
                importance=importance,
                dimension=Dimension.BEHAVIORAL if rid in behavioral else Dimension.HARD,
            )
            for rid, importance, _, _ in rows
        ),
        hard_gates=(),
    )
    evidence = EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=rid,
                classification=classification,
                strength=strength,
                entry_ids=() if classification is Classification.GAP else ("e1",),
            )
            for rid, _, classification, strength in rows
        )
    )
    return spec, evidence


def test_a_fit_reason_names_a_weak_must_have_before_a_missing_nice_to_have() -> None:
    spec, evidence = _graded_posting(
        ("r1", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r2", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("n1", Importance.NICE_TO_HAVE, Classification.GAP, Strength.NONE),
        ("r3", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.PURSUE
    assert recommendation.reasons[-1] == (
        "the record answers 66 per cent of what the posting weights, and its weakest answers "
        "are r3 (essential, weak by transferable work): Requirement r3; "
        "n1 (preferred, not answered): Requirement n1"
    )


def test_a_fit_reason_names_at_most_two_rows_and_the_must_haves_first() -> None:
    spec, evidence = _graded_posting(
        ("r1", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r2", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r3", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("n1", Importance.NICE_TO_HAVE, Classification.GAP, Strength.NONE),
        ("r4", Importance.MUST_HAVE, Classification.DIRECT, Strength.MODERATE),
        ("r5", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
    )

    reason = recommend(spec, evidence).reasons[-1]

    assert reason.endswith(
        "its weakest answers are r5 (essential, weak by transferable work): Requirement r5; "
        "r4 (essential, moderate by cited evidence): Requirement r4"
    )
    assert "n1" not in reason


def test_a_fit_reason_passes_over_a_stated_answer_to_name_a_weak_must_have() -> None:
    spec, evidence = _graded_posting(
        ("r1", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r2", Importance.MUST_HAVE, Classification.ASSERTED, Strength.NONE),
        ("r3", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r4", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
        ("r5", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r6", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.PURSUE
    assert recommendation.reasons[-1].endswith(
        "its weakest answers are r4 (essential, weak by transferable work): Requirement r4"
    )


def test_an_unmarked_posting_reason_names_its_weakest_answer() -> None:
    spec, evidence = _graded_posting(
        ("n1", Importance.NICE_TO_HAVE, Classification.DIRECT, Strength.STRONG),
        ("n2", Importance.NICE_TO_HAVE, Classification.DIRECT, Strength.STRONG),
        ("n3", Importance.NICE_TO_HAVE, Classification.DIRECT, Strength.STRONG),
        ("n4", Importance.NICE_TO_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.CAUTIOUS
    assert "no requirement is marked required" in recommendation.reasons[-1]
    assert recommendation.reasons[-1].endswith(
        "its weakest answers are n4 (preferred, weak by transferable work): Requirement n4"
    )


def test_a_skip_names_an_unanswered_must_have_once() -> None:
    spec, evidence = _graded_posting(
        ("r1", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r2", Importance.MUST_HAVE, Classification.GAP, Strength.NONE),
        ("n1", Importance.NICE_TO_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.SKIP
    assert sum("r2" in reason for reason in recommendation.reasons) == 1
    assert recommendation.reasons[-1].endswith(
        "its weakest answers are n1 (preferred, weak by transferable work): Requirement n1"
    )


def test_a_cautious_pursue_between_the_fit_lines_says_so_and_names_its_weakest_rows() -> None:
    spec, evidence = _graded_posting(
        ("r1", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r2", Importance.MUST_HAVE, Classification.DIRECT, Strength.MODERATE),
        ("r3", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
        ("r4", Importance.MUST_HAVE, Classification.DIRECT, Strength.WEAK),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.CAUTIOUS
    assert recommendation.reasons == (
        "the record answers 55 per cent of what the posting weights, above the deprioritize "
        "line of 50 and below the apply-now line of 60, and its weakest answers are "
        "r3 (essential, weak by transferable work): Requirement r3; "
        "r4 (essential, weak by cited evidence): Requirement r4",
    )


_STRONG_CORES = tuple(
    (rid, Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG)
    for rid in ("r1", "r2", "r3")
)


def test_two_core_must_haves_answered_weakly_by_transferable_work_cap_a_pursue() -> None:
    spec, evidence = _graded_posting(
        *_STRONG_CORES,
        ("r4", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
        ("r5", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.CAUTIOUS
    assert recommendation.reasons == (
        "2 essential requirements are answered only weakly by transferable work: "
        "r4 (essential, weak by transferable work): Requirement r4; "
        "r5 (essential, weak by transferable work): Requirement r5",
    )


def test_one_core_must_have_answered_weakly_by_transferable_work_still_pursues() -> None:
    spec, evidence = _graded_posting(
        *_STRONG_CORES,
        ("r4", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
        ("r5", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.MODERATE),
    )

    assert recommend(spec, evidence).verdict is Verdict.PURSUE


def test_a_behavioral_must_have_answered_by_how_it_is_written_leaves_fit_unchanged() -> None:
    rows = (
        ("r1", Importance.MUST_HAVE, Classification.DIRECT, Strength.STRONG),
        ("r2", Importance.MUST_HAVE, Classification.DIRECT, Strength.MODERATE),
        ("r3", Importance.MUST_HAVE, Classification.DIRECT, Strength.WEAK),
    )
    soft = ("b1", Importance.MUST_HAVE, Classification.TAILORING_ONLY, Strength.STRONG)

    without = recommend(*_graded_posting(*rows))
    with_soft = recommend(*_graded_posting(*rows, soft, behavioral=frozenset({"b1"})))

    assert with_soft == without
    assert without.reasons[-1].startswith("the record answers 63 per cent")


def test_a_behavioral_must_have_the_record_cannot_answer_caps_nothing() -> None:
    spec, evidence = _graded_posting(
        *_STRONG_CORES,
        ("b1", Importance.MUST_HAVE, Classification.GAP, Strength.NONE),
        behavioral=frozenset({"b1"}),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.PURSUE
    assert not any("b1" in reason for reason in recommendation.reasons)


def test_a_hard_must_have_the_record_cannot_answer_still_caps_a_pursue() -> None:
    spec, evidence = _graded_posting(
        *_STRONG_CORES,
        ("r4", Importance.MUST_HAVE, Classification.GAP, Strength.NONE),
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is Verdict.CAUTIOUS
    assert (
        "r4 is stated as essential and the record does not answer it" in (recommendation.reasons[0])
    )


DEGREE_GATE = "Bachelor's degree and 5+ years of experience"
LEADERSHIP_GATE = "At least two years leading an engineering team"


def _record_gate_posting(*, restated: bool) -> JobSpec:
    """A posting with two verified blocking record gates, split into sub-requirements."""
    requirements = [
        Requirement(id="r01", text="5+ years of experience.", importance=Importance.MUST_HAVE),
        Requirement(id="r02", text="Proficiency in SQL.", importance=Importance.MUST_HAVE),
    ]
    if restated:
        requirements += [
            Requirement(id="r03", text=f"{DEGREE_GATE}.", importance=Importance.MUST_HAVE),
            Requirement(id="r04", text=f"{LEADERSHIP_GATE}.", importance=Importance.MUST_HAVE),
        ]
    gates = (
        HardGate(
            kind="degree",
            text=DEGREE_GATE,
            blocking=True,
            quote_or_span=DEGREE_GATE,
            source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(DEGREE_GATE)),
        ),
        HardGate(
            kind="leadership",
            text=LEADERSHIP_GATE,
            blocking=True,
            quote_or_span=LEADERSHIP_GATE,
            source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(LEADERSHIP_GATE)),
        ),
    )
    return JobSpec(
        title="Engineering Manager",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=tuple(requirements),
        hard_gates=gates,
    )


def _graded(spec: JobSpec, **overrides: Classification) -> EvidenceMap:
    """Grade every requirement DIRECT except the sub-requirement and any override."""
    graded = {"r01": Classification.TRANSFERABLE, **overrides}
    return EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=requirement.id,
                classification=graded.get(requirement.id, Classification.DIRECT),
                strength=(
                    Strength.NONE
                    if graded.get(requirement.id) is Classification.GAP
                    else Strength.STRONG
                ),
                entry_ids=() if graded.get(requirement.id) is Classification.GAP else ("e1",),
            )
            for requirement in spec.requirements
        )
    )


def test_record_gates_restated_in_their_own_words_are_answered_by_those_restatements() -> None:
    spec = _record_gate_posting(restated=True)

    recommendation = recommend(spec, _graded(spec))

    assert recommendation.verdict is Verdict.PURSUE
    assert not any("paired nothing to it" in reason for reason in recommendation.reasons)


def test_record_gates_nothing_restates_leave_the_posting_capped() -> None:
    spec = _record_gate_posting(restated=False)

    assert recommend(spec, _graded(spec)).verdict is Verdict.CAUTIOUS


@pytest.mark.parametrize(
    ("kind", "words"),
    [("minimum_years", "minimum years"), ("other", "a condition"), ("degree", "degree")],
)
def test_an_unpaired_gate_reason_names_its_kind_in_words(kind: GateKind, words: str) -> None:
    text = "Must hold the stated qualification"
    gate = HardGate(
        kind=kind,
        text=text,
        blocking=True,
        quote_or_span=text,
        source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(text)),
    )
    spec = JobSpec(
        title="Platform Engineer",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(),
        hard_gates=(gate,),
    )

    reasons = recommend(spec, EvidenceMap()).reasons

    assert f"the posting states {words} absolutely and this run paired nothing" in " ".join(reasons)


def test_a_restated_record_gate_the_record_fails_is_a_skip() -> None:
    spec = _record_gate_posting(restated=True)

    recommendation = recommend(spec, _graded(spec, r03=Classification.GAP))

    assert recommendation.verdict is Verdict.SKIP


def test_a_gate_skip_still_names_the_boundary_the_occupation_falls_under() -> None:
    """Unwanted work reads apart from a waivable gate, with the posting quote capped."""
    boundary = "routine data entry without a clinical domain"
    quote = "Leads the data entry team " + "and keeps every intake ledger current " * 6
    spec = _record_gate_posting(restated=True).model_copy(
        update={"excluded_boundary": boundary, "excluded_boundary_quote": quote}
    )

    recommendation = recommend(spec, _graded(spec, r03=Classification.GAP), boundaries=(boundary,))

    assert recommendation.verdict is Verdict.SKIP
    *gates, ruled_out = recommendation.reasons
    assert gates
    assert ruled_out.startswith(
        f"the posting's core occupation is work your search rules out: {boundary} "
        '(the posting: "Leads the data entry team'
    )
    assert ruled_out.endswith('...")')
    assert quote.strip() not in ruled_out


@pytest.mark.parametrize(
    ("strength", "verdict"),
    [
        (Strength.WEAK, Verdict.CAUTIOUS),
        (Strength.MODERATE, Verdict.PURSUE),
        (Strength.STRONG, Verdict.PURSUE),
    ],
)
def test_a_blocking_years_minimum_answered_only_weakly_caps_a_pursue(
    strength: Strength, verdict: Verdict
) -> None:
    years = "At least 8 years of clinical research experience"
    spec = JobSpec(
        title="Clinical Data Lead",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(
            Requirement(id="r01", text="Proficiency in SQL.", importance=Importance.MUST_HAVE),
            Requirement(id="r02", text=f"{years}.", importance=Importance.MUST_HAVE),
        ),
        hard_gates=(
            HardGate(
                kind="minimum_years",
                text=years,
                blocking=True,
                quote_or_span=years,
                source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(years)),
            ),
        ),
    )
    evidence = EvidenceMap(
        matches=(
            _answered("r01"),
            Match(
                requirement_id="r02",
                classification=Classification.TRANSFERABLE,
                strength=strength,
                entry_ids=("e1",),
            ),
        )
    )

    assert recommend(spec, evidence).verdict is verdict


def test_a_blocking_years_minimum_nothing_restates_caps_a_pursue() -> None:
    years = "At least 8 years of clinical research experience"
    spec = JobSpec(
        title="Clinical Data Lead",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(
            Requirement(id="r01", text="Proficiency in SQL.", importance=Importance.MUST_HAVE),
        ),
        hard_gates=(
            HardGate(
                kind="minimum_years",
                text=years,
                blocking=True,
                quote_or_span=years,
                source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(years)),
            ),
        ),
    )

    recommendation = recommend(spec, EvidenceMap(matches=(_answered("r01"),)))

    assert recommendation.verdict is Verdict.CAUTIOUS
    assert [reason for reason in recommendation.reasons if years in reason] == [
        "the posting states minimum years absolutely and this run paired nothing to it, "
        f"so it is unestablished rather than failed: {years}"
    ]


def _one_open_must_have(text: str) -> JobSpec:
    """A posting whose four other must-haves the record answers, r05 being ``text``."""
    answered = ("Proficiency in SQL.", "Data modeling.", "Stakeholder reporting.", "Python.")
    return JobSpec(
        title="Data Analyst",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=tuple(
            Requirement(id=f"r{index:02d}", text=line, importance=Importance.MUST_HAVE)
            for index, line in enumerate((*answered, text), start=1)
        ),
        hard_gates=(),
    )


LOGISTICS = (
    "Must be able to travel occasionally, if required, to collaborate with distributed teams.",
    "Willingness to travel up to 25% of the time.",
    "Occasional travel to client sites.",
    "Domestic and international travel as needed.",
    "Travel required for quarterly team meetings.",
    "Ability to travel overnight to customer offices.",
    "A download speed of 25 Mbps and an upload speed of 10 Mbps is required.",
    "Reliable high-speed internet connection.",
    "Stable home internet suitable for video calls.",
    "A dedicated, quiet home office.",
    "A private, distraction-free workspace.",
    "Home office setup with a second monitor.",
    "Attend in-office events that will require travel to and from the Springfield office.",
)
RECORD_SKILLS = (
    "Hands-on experience with Snowflake.",
    "Experience in the travel industry.",
    "Travel nurse experience in acute care.",
    "Business travel management software experience.",
    "Travel and expense reporting in SAP Concur.",
    "Network engineering experience.",
    "Bandwidth planning for WAN links.",
    "Experience building home health software.",
    "Remote patient monitoring experience.",
    "Experience troubleshooting internet connectivity for clinics.",
    "Experience deploying high-speed broadband networks.",
    "Experience in international travel medicine.",
    "Knowledge of domestic travel insurance products.",
    "Travel expense reporting experience.",
    "Administering a dedicated Databricks workspace.",
    "Managing a private Slack workspace.",
    "Experience delivering reliable internet services.",
    "Building stable internet-of-things devices.",
    "Optimizing download speeds for web applications.",
    "Delivered 15% travel cost savings.",
)


@pytest.mark.parametrize(
    ("gapped", "verdict"),
    [
        ("Available to work East Coast hours occasionally.", Verdict.PURSUE),
        ("Fluency in French.", Verdict.PURSUE),
        *((text, Verdict.PURSUE) for text in LOGISTICS),
        *((text, Verdict.CAUTIOUS) for text in RECORD_SKILLS),
    ],
)
def test_only_a_must_have_the_record_could_answer_caps_a_pursue(
    gapped: str, verdict: Verdict
) -> None:
    spec = _one_open_must_have(gapped)
    evidence = EvidenceMap(
        matches=(
            *(_answered(f"r{index:02d}") for index in range(1, 5)),
            Match(
                requirement_id="r05",
                classification=Classification.GAP,
                strength=Strength.NONE,
            ),
        )
    )

    recommendation = recommend(spec, evidence)

    assert recommendation.verdict is verdict
    assert not any("r05 (essential, not answered)" in r for r in recommendation.reasons)


@pytest.mark.parametrize("text", ["Available to work East Coast hours occasionally.", *LOGISTICS])
def test_an_unestablished_hours_or_logistics_must_have_leaves_the_run_settled(text: str) -> None:
    spec = _one_open_must_have(text)
    evidence = EvidenceMap(
        matches=(*(_answered(f"r{index:02d}") for index in range(1, 5)), _unestablished("r05"))
    )

    assert recommend(spec, evidence).verdict is Verdict.PURSUE


@pytest.mark.parametrize(
    ("text", "words"),
    [
        (
            "Fluency in French.",
            "a condition about working hours or language that a career record cannot answer",
        ),
        (
            LOGISTICS[1],
            "a condition about travel, connectivity, or a workspace that you or your "
            "search parameters answer rather than the record",
        ),
        (
            LOGISTICS[6],
            "a condition about travel, connectivity, or a workspace that you or your "
            "search parameters answer rather than the record",
        ),
    ],
    ids=["language", "travel", "connectivity"],
)
def test_a_must_have_the_person_answers_is_named_for_who_answers_it(text: str, words: str) -> None:
    spec = _one_open_must_have(text)
    evidence = EvidenceMap(
        matches=(
            *(_answered(f"r{index:02d}") for index in range(1, 5)),
            Match(requirement_id="r05", classification=Classification.GAP, strength=Strength.NONE),
        )
    )

    reasons = recommend(spec, evidence).reasons

    assert f"r05 is stated as essential, {words}: {text}" in reasons


def _unestablished(requirement_id: str) -> Match:
    """The row an incomplete answer normalizes to."""
    return Match(
        requirement_id=requirement_id,
        classification=Classification.GAP,
        strength=Strength.NONE,
        evidence_status=EvidenceStatus.UNRESOLVED,
        gap_class=GapClass.INCOMPLETE_EVIDENCE,
    )


def _answered(requirement_id: str) -> Match:
    return Match(
        requirement_id=requirement_id,
        classification=Classification.DIRECT,
        strength=Strength.STRONG,
        entry_ids=("e1",),
    )


def _transferable_weak(requirement_id: str) -> Match:
    return Match(
        requirement_id=requirement_id,
        classification=Classification.TRANSFERABLE,
        strength=Strength.WEAK,
        entry_ids=("e1",),
    )


@pytest.mark.parametrize("text", ["Fluency in French.", LOGISTICS[1], LOGISTICS[7]])
def test_a_person_answered_must_have_answered_weakly_is_no_thin_core(text: str) -> None:
    spec = _one_open_must_have(text)
    extra = Requirement(id="r06", text="Clinical data review.", importance=Importance.MUST_HAVE)
    spec = spec.model_copy(update={"requirements": (*spec.requirements, extra)})
    evidence = EvidenceMap(
        matches=(
            *(_answered(f"r{index:02d}") for index in range(1, 5)),
            _transferable_weak("r05"),
            _transferable_weak("r06"),
        )
    )

    reasons = recommend(spec, evidence).reasons

    assert not any("answered only weakly by transferable work" in reason for reason in reasons)


def test_two_behavioral_must_haves_answered_weakly_by_transferable_work_cap_nothing() -> None:
    spec, evidence = _graded_posting(
        *_STRONG_CORES,
        ("b1", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
        ("b2", Importance.MUST_HAVE, Classification.TRANSFERABLE, Strength.WEAK),
        behavioral=frozenset({"b1", "b2"}),
    )

    assert recommend(spec, evidence).verdict is Verdict.PURSUE


def test_two_core_must_haves_answered_weakly_by_cited_evidence_are_no_thin_cores() -> None:
    spec, evidence = _graded_posting(
        *_STRONG_CORES,
        ("r4", Importance.MUST_HAVE, Classification.DIRECT, Strength.WEAK),
        ("r5", Importance.MUST_HAVE, Classification.DIRECT, Strength.WEAK),
    )

    reasons = recommend(spec, evidence).reasons

    assert not any("answered only weakly by transferable work" in reason for reason in reasons)


@pytest.mark.parametrize(
    "unsettled",
    [
        ((f"{DEGREE_GATE}.", _unestablished("r03")),),
        (("Bachelor's degree.", _unestablished("r03")),),
    ],
    ids=["unestablished-restatement", "unestablished-part"],
)
def test_a_compound_gate_one_part_of_which_is_unsettled_is_never_a_pursue(
    unsettled: tuple[tuple[str, Match], ...],
) -> None:
    """Answering the years clause does not answer a degree the run left open."""
    spec = JobSpec(
        title="Data Engineer",
        company="Initrode",
        seniority=None,
        location="Remote",
        keywords=(),
        origin="",
        requirements=(
            Requirement(id="r01", text="5+ years of experience.", importance=Importance.MUST_HAVE),
            Requirement(id="r02", text="Proficiency in SQL.", importance=Importance.MUST_HAVE),
            *(
                Requirement(id=match.requirement_id, text=text, importance=Importance.NICE_TO_HAVE)
                for text, match in unsettled
            ),
        ),
        hard_gates=(
            HardGate(
                kind="degree",
                text=DEGREE_GATE,
                blocking=True,
                quote_or_span=DEGREE_GATE,
                source_span=Span(status=SpanStatus.VERIFIED, start=0, end=len(DEGREE_GATE)),
            ),
        ),
    )
    evidence = EvidenceMap(
        matches=(_answered("r01"), _answered("r02"), *(match for _, match in unsettled))
    )

    assert recommend(spec, evidence).verdict is not Verdict.PURSUE
