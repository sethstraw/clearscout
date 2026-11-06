"""The live search-vocabulary task: its dataset, its scorer, and its request parity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from evals.inspect.product_questions import ProductQuestion
from evals.inspect.runner import dataset_samples
from evals.inspect.scorers.search_vocabulary import gold_labels, proposal_usefulness
from evals.inspect.solvers.search_vocabulary_live import (
    case_inventory,
    case_parameters,
    proposal_result,
    propose,
)
from evals.inspect.tasks import search_vocabulary_live_task as task
from evals.support.client import build_scripted_client
from evals.support.scripted_provider import ScriptedProvider as EvalScriptedProvider

from clearscout.application.config import Stage
from clearscout.application.discovery.derived_vocabulary_rules import (
    ProposedArchetype,
    ProposedTitle,
)
from clearscout.application.discovery.search_vocabulary import (
    ProposedSearch,
    SearchVocabularyService,
)
from clearscout.application.master_cv import MasterCvService
from clearscout.application.tailoring.stages.context import citable_entries
from clearscout.application.tasks import SearchVocabularyPayload
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.plan import RequestPlan
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedProvider


def _samples() -> list[dict[str, Any]]:
    return dataset_samples(task.DATASET_PATH, split="development")


def _case(case_id: str) -> dict[str, Any]:
    return next(row for row in _samples() if row["id"] == case_id)


def _title(name: str, *ids: str) -> ProposedTitle:
    return ProposedTitle(title=name, reason="The record shows it.", evidence_ids=list(ids))


def _archetype(name: str, *ids: str) -> ProposedArchetype:
    return ProposedArchetype(name=name, because="The record shows it.", evidence_ids=list(ids))


def _score(
    case_id: str, titles: list[ProposedTitle], archetypes: list[ProposedArchetype]
) -> dict[str, Any]:
    proposed = ProposedSearch(titles=titles, archetypes=archetypes)
    score: dict[str, Any] = proposal_usefulness()(_case(case_id), proposal_result(proposed))
    return score


GOOD_TITLES = [
    _title("Data Platform Engineer", "b-b279601d"),
    _title("Streaming Data Engineer", "b-b279601d"),
]
GOOD_ARCHETYPES = [_archetype("Healthcare data companies", "ctx-d88634a9")]


def _outcomes(score: dict[str, Any], axis: str = "titles") -> dict[str, str]:
    return {row["name"]: row["outcome"] for row in score["metadata"][axis]["proposals"]}


def test_the_task_declares_the_stage_and_product_question() -> None:
    assert Stage.SEARCH_VOCABULARY.value == task.STAGE
    assert task.PRODUCT_QUESTION is ProductQuestion.SEARCH_VOCABULARY_PROPOSAL_USEFULNESS


def test_every_case_validates_and_cites_only_ids_its_cv_has() -> None:
    samples = _samples()
    assert len(samples) >= 12
    for sample in samples:
        gold = gold_labels(sample)
        known = {entry.id for entry in citable_entries(case_inventory(sample))}
        for axis in ("titles", "archetypes"):
            for item in gold[axis]["acceptable"]:
                assert set(item["evidence_ids"]) <= known, (sample["id"], item["name"])


def test_a_label_naming_an_id_the_cv_lacks_is_refused() -> None:
    sample = _case("excluded-titles")
    broken = {**sample, "gold": {**sample["gold"]}}
    broken["gold"]["titles"] = {
        "acceptable": [{"name": "Data Platform Engineer", "evidence_ids": ["b-invented"]}]
    }
    with pytest.raises(ValueError, match="cites an id its CV lacks"):
        gold_labels(broken)


def test_grounded_acceptable_proposals_pass() -> None:
    score = _score("excluded-titles", GOOD_TITLES, GOOD_ARCHETYPES)
    assert score["value"] is True
    assert score["metadata"]["precision"] == 1.0
    assert score["metadata"]["citation_support"] == 1.0
    assert score["metadata"]["program_refused"] == 0
    assert 0 < score["metadata"]["recall"] < 1.0


def test_an_off_level_title_counts_against_precision() -> None:
    score = _score(
        "level-up",
        [
            _title("Head of Data Engineering", "ctx-39c2af8c"),
            _title("Senior Data Engineer", "ctx-39c2af8c"),
        ],
        [],
    )
    assert _outcomes(score)["Senior Data Engineer"] == "unacceptable"
    assert score["metadata"]["precision"] == 0.5
    assert score["value"] is False
    assert score["checks"]["failed"] == ["precision"]


def test_unlabeled_proposals_are_reported_without_sinking_precision() -> None:
    score = _score(
        "excluded-titles",
        [
            GOOD_TITLES[0],
            _title("Orchard Logistics Coordinator", "b-b279601d"),
            _title("Harbor Survey Technician", "b-b279601d"),
        ],
        [],
    )
    assert score["value"] is True
    assert score["metadata"]["precision"] == 1.0
    assert score["metadata"]["unlabeled"] == 2
    assert score["metadata"]["unlabeled_share"] == pytest.approx(2 / 3)
    assert score["checks"] == {"failed": [], "warned": ["unlabeled_proposals"]}


def test_a_call_with_no_labeled_proposal_fails_by_name() -> None:
    score = _score("excluded-titles", [_title("Harbor Survey Technician", "b-b279601d")], [])
    assert score["value"] is False
    assert score["metadata"]["precision"] is None
    assert "no_labeled_proposals" in score["checks"]["failed"]


def test_an_excluded_title_is_scored_excluded_and_the_program_refuses_it() -> None:
    score = _score(
        "excluded-titles", [*GOOD_TITLES, _title("Senior Data Analyst", "b-636c4345")], []
    )
    rows = {row["name"]: row for row in score["metadata"]["titles"]["proposals"]}
    assert rows["Senior Data Analyst"]["outcome"] == "excluded"
    assert rows["Senior Data Analyst"]["program_refused_because"] == "excluded_title"
    assert score["metadata"]["program_refused"] == 1


def test_a_proposal_citing_a_non_supporting_entry_fails_citation_support() -> None:
    score = _score(
        "excluded-titles",
        [
            _title("Data Platform Engineer", "edu-f09706a7"),
            _title("Streaming Data Engineer", "b-b279601d"),
        ],
        [_archetype("Healthcare data companies", "pub-4b625434")],
    )
    assert score["metadata"]["precision"] == 1.0
    assert score["metadata"]["citation_support"] == pytest.approx(1 / 3)
    assert score["value"] is False


def test_a_repeat_of_a_stated_title_is_scored_and_refused() -> None:
    score = _score(
        "stated-titles",
        [
            _title("Telemetry Platform Engineer", "b-27ebe4ab"),
            _title("principal platform engineer", "ctx-31aae1ed"),
        ],
        [_archetype("Public transit agencies", "ctx-31aae1ed")],
    )
    assert _outcomes(score)["principal platform engineer"] == "repeats_stated"
    assert _outcomes(score, "archetypes")["Public transit agencies"] == "repeats_stated"
    assert score["metadata"]["program_refused"] == 2
    assert score["value"] is False


def test_a_reason_that_inflates_a_specialty_into_leadership_fails_by_name() -> None:
    grounded = _archetype("AI model developers", "b-eaaf8318")
    inflated = ProposedArchetype(
        name="AI model developers",
        because="Led AI evaluation for model-drafted documentation.",
        evidence_ids=["b-eaaf8318"],
    )
    titles = [_title("AI Evaluation Specialist", "b-eaaf8318")]
    assert _score("specialty-inflation", titles, [grounded])["value"] is True
    score = _score("specialty-inflation", titles, [inflated])
    assert score["value"] is False
    assert score["checks"]["failed"] == ["unsupported_claim"]
    (row,) = score["metadata"]["archetypes"]["proposals"]
    assert row["unsupported_claim"].startswith("The record built one evaluation set")


@pytest.mark.parametrize(
    "reason",
    [
        "Leads AI evaluation for documentation releases.",
        "Headed up evaluation of model-drafted pages.",
        "Directed model evaluation for the product team.",
    ],
)
def test_a_title_reason_claiming_evaluation_leadership_fails_by_name(reason: str) -> None:
    inflated = ProposedTitle(title="AI Evaluation Lead", reason=reason, evidence_ids=["b-eaaf8318"])
    archetypes = [_archetype("AI model developers", "b-eaaf8318")]
    for case_id in ("specialty-inflation", "domain-switch"):
        score = _score(case_id, [inflated], archetypes)
        assert score["checks"]["failed"] == ["unsupported_claim"], case_id
        (row,) = score["metadata"]["titles"]["proposals"]
        assert row["outcome"] == "acceptable"
        assert row["unsupported_claim"].startswith("The record built one evaluation set")


@pytest.mark.parametrize(
    "because",
    [
        "Leading AI labs review model-drafted documentation before release.",
        "Companies leading the AI tooling market need documentation evaluation.",
        "Developer tools companies leading the LLM tooling space.",
        "AI labs where the head of AI evaluation needs release checks on drafted pages.",
    ],
)
def test_a_because_describing_the_employer_is_not_a_claim(because: str) -> None:
    archetype = ProposedArchetype(
        name="AI model developers", because=because, evidence_ids=["b-eaaf8318"]
    )
    for case_id in ("specialty-inflation", "domain-switch"):
        score = _score(case_id, [_title("AI Evaluation Lead", "b-eaaf8318")], [archetype])
        assert score["value"] is True, case_id
        assert score["metadata"]["unsupported_claims"] == 0, case_id


@pytest.mark.parametrize(
    "reason",
    [
        "A role leading evaluation of model output, built on the record's evaluation set.",
        "Moves toward the evaluation leadership the thesis asks for.",
        "Built the documentation evaluation set; a step toward leading evaluation for AI products.",
    ],
)
def test_a_title_reason_describing_the_target_role_is_not_a_claim(reason: str) -> None:
    title = ProposedTitle(title="AI Evaluation Lead", reason=reason, evidence_ids=["b-eaaf8318"])
    archetypes = [_archetype("AI model developers", "b-eaaf8318")]
    for case_id in ("specialty-inflation", "domain-switch"):
        score = _score(case_id, [title], archetypes)
        assert score["value"] is True, case_id
        assert score["metadata"]["unsupported_claims"] == 0, case_id


@pytest.mark.parametrize(
    "claim", [{"terms": ["led ai"]}, {"terms": "led ai", "why": "No AI function was led."}]
)
def test_an_unsupported_claim_label_without_a_term_list_or_a_why_is_refused(
    claim: dict[str, Any],
) -> None:
    sample = _case("specialty-inflation")
    broken = {**sample, "gold": {**sample["gold"], "unsupported_claims": [claim]}}
    with pytest.raises(ValueError, match="unsupported claim without terms or a why"):
        gold_labels(broken)


def test_proposals_citing_no_delivered_work_are_counted_without_failing() -> None:
    score = _score(
        "context-line-level",
        [
            _title("Rail Reliability Engineer", "ctx-4bb19615", "skl-37311ed5"),
            _title("Condition Monitoring Engineer", "ctx-4bb19615", "b-593bd0a9"),
        ],
        [_archetype("Rail operators", "ctx-4bb19615")],
    )
    assert score["value"] is True
    assert score["metadata"]["context_only_citations"] == 2
    flagged = {
        row["name"]
        for axis in ("titles", "archetypes")
        for row in score["metadata"][axis]["proposals"]
        if row.get("context_only_citation")
    }
    assert flagged == {"Rail Reliability Engineer", "Rail operators"}


def test_a_colleague_level_title_counts_against_precision() -> None:
    score = _score(
        "context-line-level",
        [
            _title("Rail Reliability Engineer", "b-1f7be7d0"),
            _title("Senior Safety Engineer", "ctx-4bb19615"),
        ],
        [],
    )
    assert _outcomes(score)["Senior Safety Engineer"] == "unacceptable"
    assert score["metadata"]["precision"] == 0.5
    assert score["checks"]["failed"] == ["precision"]


def _empty_answer(schema: object) -> dict[str, Any]:
    return {"titles": [], "archetypes": [], "capabilities": [], "domains": []}


def _production_plan(database: Database, tmp_path: Path, sample: dict[str, Any]) -> RequestPlan:
    """Return the plan a fresh workspace holding this case's CV and parameters sends."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "clearscout.toml").write_text("pages = 2\n", encoding="utf-8")
    storage = runtime_storage(database, tmp_path / "artifacts")
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="vocab", name="Vocab", root_path=str(root))
        .id
    )
    SearchParametersService(database).save(workspace_id, case_parameters(sample))
    cv = Path(task.DATASET_PATH).parents[1] / "cvs" / sample["input"]["cv"]
    MasterCvService(database, storage).import_source(
        workspace_id=workspace_id, filename="CV_Master.md", content=cv.read_bytes()
    )
    provider = ScriptedProvider({"SearchVocabulary": _empty_answer})
    SearchVocabularyService(database, storage=storage, provider=provider).search(
        workspace_id, SearchVocabularyPayload()
    )
    ((_, plan),) = provider.calls
    return plan


@pytest.mark.parametrize(
    "case_id", ["excluded-titles", "level-up", "domain-switch", "stated-titles"]
)
def test_the_solver_sends_the_request_production_sends(
    durable_database: Database, tmp_path: Path, case_id: str
) -> None:
    sample = _case(case_id)
    produced = _production_plan(durable_database, tmp_path, sample)
    provider = EvalScriptedProvider({"SearchVocabulary": _empty_answer})
    propose(build_scripted_client(provider), {"id": sample["id"], "input": sample["input"]})
    ((_, sent),) = provider.calls
    assert sent.stage is produced.stage is Stage.SEARCH_VOCABULARY
    assert sent.instructions.text == produced.instructions.text
    assert sent.task.text == produced.task.text
    assert sent.evidence is not None and produced.evidence is not None
    assert sent.evidence.text == produced.evidence.text
    assert sent.search is None and produced.search is None
    assert sent.max_tokens == produced.max_tokens
