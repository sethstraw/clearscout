"""A live evaluation bills its calls, and reports what the run established.

Billing lands in the workspace ledger it spends against, a sample whose
requirement the run could not establish says so rather than reading as a gap,
and a ledger write that fails is reported rather than thrown over the paid
answer it could not record.
"""

from __future__ import annotations

from typing import Any

import pytest
from evals.inspect.scorers.match_classification import classification_match
from evals.inspect.solvers import clearscout_live, relevance_live
from evals.support.inventory import synthetic_guardrails, synthetic_inventory
from sqlalchemy import select

from clearscout.application.config import ProviderName, Settings
from clearscout.application.job_search.models import RelevanceReviewBatchResponse
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.prompts import load
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.domain import EvidenceStatus, GapClass
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models.tailoring import CostRecord, ModelCall
from clearscout.infrastructure.persistence.models.workspaces import Workspace
from clearscout.shared.costs import CallManifest, Ledger, Usage


def _workspace(url: str, slugs: list[str]) -> None:
    database = Database(url)
    try:
        with database.session() as session:
            for slug in slugs:
                session.add(Workspace(slug=slug, name=slug))
    finally:
        database.dispose()


def _manifest() -> CallManifest:
    return CallManifest(
        stage="match",
        provider="openai",
        model="gpt-5.6-terra",
        usage=Usage(input_tokens=1200, output_tokens=300),
        validated=True,
    )


def _solver(
    database: Database,
    workspace_id: str,
    ledger: Ledger | None = None,
    inventory: CvInventory | None = None,
    guardrails: Guardrails | None = None,
) -> clearscout_live._ClearScoutLiveSolver:
    return clearscout_live._ClearScoutLiveSolver(
        run_metadata={
            "provider": "openai",
            "prompt_suite_fingerprint": clearscout_live._prompt_fingerprint(
                {name: load(name) for name in clearscout_live._MATCH_PROMPTS}
            ),
        },
        inventory=inventory,  # type: ignore[arg-type]
        guardrails=guardrails,  # type: ignore[arg-type]
        provider_name=None,  # type: ignore[arg-type]
        settings=Settings(provider=ProviderName.OPENAI),
        prompt_snapshot={},
        ledger=ledger if ledger is not None else Ledger(),
        database=database,
        workspace_id=workspace_id,
    )


def test_billing_target_refuses_when_no_single_workspace_owns_the_spend(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workspaces give the run no unambiguous ledger, so it refuses rather than guess."""
    _workspace(durable_database_url, ["one", "two"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)

    with pytest.raises(RuntimeError, match="one active workspace"):
        clearscout_live.billing_target()


def test_billing_target_refuses_without_a_database_to_bill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live run must name the ledger it spends against."""
    monkeypatch.delenv("CLEARSCOUT_EVAL_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="workspace database to bill"):
        clearscout_live.billing_target()


def test_a_priced_call_reaches_the_workspace_ledger(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spending real money leaves a model call and its cost record behind.

    Without this the workspace's own total understates what left the account.
    """
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    sample = Ledger()
    sample.record(_manifest())

    _solver(database, workspace_id)._bill(sample)

    try:
        with database.session() as session:
            calls = list(session.scalars(select(ModelCall)))
            costs = list(session.scalars(select(CostRecord)))
    finally:
        database.dispose()

    assert len(calls) == 1
    assert calls[0].stage == "match"
    assert calls[0].workspace_id == workspace_id
    assert calls[0].origin == "eval.match_live"
    assert len(costs) == 1
    assert costs[0].model_call_id == calls[0].id


def test_a_live_requirement_the_run_never_established_is_not_credited(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paid row carrying no outcome fails against a frozen GAP label."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    unestablished = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                classification=Classification.GAP,
                strength=Strength.NONE,
                note="Model result was missing; treated as a gap.",
                evidence_status=EvidenceStatus.UNRESOLVED,
                gap_class=GapClass.INCOMPLETE_EVIDENCE,
            ),
        )
    )
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "token")
    monkeypatch.setattr(clearscout_live, "build_live_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(clearscout_live, "match", lambda *args, **kwargs: unestablished)
    monkeypatch.setattr(clearscout_live, "sample_provenance", lambda *args, **kwargs: {})
    inventory = synthetic_inventory()
    solver = _solver(
        database,
        workspace_id,
        inventory=inventory,
        guardrails=synthetic_guardrails(),
    )

    try:
        result = solver(
            {
                "input": "Ship a Python service.",
                "dimension": "hard",
                "cv_hash": inventory.content_hash,
            }
        )
    finally:
        database.dispose()

    assert result["completion"] == "GAP"
    assert result["metadata"]["outcome_complete"] is False
    assert result["metadata"]["gap_class"] == "incomplete_evidence"
    assert classification_match()({"target": "GAP"}, result)["value"] is False


def _paid_client(*args: Any, **kwargs: Any) -> object:
    """Stand in for the live client, recording the call the sample already paid for."""
    del args
    ledger: Ledger = kwargs["ledger"]
    ledger.record(_manifest())
    return object()


def _match_sample(inventory: CvInventory) -> dict[str, Any]:
    return {
        "input": "Ship a Python service.",
        "dimension": "hard",
        "cv_hash": inventory.content_hash,
    }


def _answered() -> EvidenceMap:
    return EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=("e1",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                note="Shipped the Python service end to end.",
            ),
        )
    )


def test_a_live_answer_keeps_the_models_note_whatever_its_class(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer reads why the model chose DIRECT, not only why it chose GAP."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    solver = _paid_match_solver(database, workspace_id, monkeypatch)

    try:
        result = solver(_match_sample(solver.inventory))
    finally:
        database.dispose()

    assert result["completion"] == "DIRECT"
    assert "gap_class" not in result["metadata"]
    scored = classification_match()({"target": "DIRECT"}, result)
    assert scored["metadata"]["note"] == "Shipped the Python service end to end."


def _paid_match_solver(
    database: Database, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> clearscout_live._ClearScoutLiveSolver:
    """Return a match solver whose paid call is answered without a provider."""
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "token")
    monkeypatch.setattr(clearscout_live, "build_live_client", _paid_client)
    monkeypatch.setattr(clearscout_live, "match", lambda *args, **kwargs: _answered())
    return _solver(
        database,
        workspace_id,
        inventory=synthetic_inventory(),
        guardrails=synthetic_guardrails(),
    )


def _refuse_billing(error: BaseException) -> Any:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise error

    return refuse


class _EmptyReviewClient:
    """A relevance client that answers with no recommendation and costs nothing more."""

    def __init__(self) -> None:
        self.settings = Settings(provider=ProviderName.OPENAI)

    def complete(self, plan: Any, schema: Any) -> RelevanceReviewBatchResponse:
        """Return the empty batch answer the shipped validation then rejects."""
        del plan, schema
        return RelevanceReviewBatchResponse(recommendations=[])


def _paid_relevance_client(kwargs: dict[str, Any]) -> _EmptyReviewClient:
    """Record the paid call on the sample ledger and answer without a provider."""
    ledger: Ledger = kwargs["ledger"]
    ledger.record(_manifest())
    return _EmptyReviewClient()


def _relevance_sample() -> dict[str, Any]:
    return {
        "id": "relevance-1",
        "input": {
            "search_context": "Backend engineering roles that ship Python services.",
            "posting": {
                "id": "p1",
                "employer": "Northwind Analytics",
                "title": "Backend Engineer",
                "location": "Remote",
                "body": "We build Python services and value clear written reasoning.",
            },
        },
    }


def test_a_paid_sample_survives_a_ledger_write_that_failed(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The money is already spent, so a failed ledger write must not destroy the answer."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    solver = _paid_match_solver(database, workspace_id, monkeypatch)
    monkeypatch.setattr(
        clearscout_live, "record_calls", _refuse_billing(RuntimeError("database is locked"))
    )

    try:
        result = solver(_match_sample(solver.inventory))
    finally:
        database.dispose()

    assert result["completion"] == "DIRECT"
    assert result["metadata"]["measurements"]
    assert result["metadata"]["actual_dollars"] == result["metadata"]["cumulative_dollars"]


def test_an_unbilled_sample_names_the_row_a_reader_must_chase(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spend the ledger refused is reported with the origin, amount, and failure."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    solver = _paid_match_solver(database, workspace_id, monkeypatch)
    monkeypatch.setattr(
        clearscout_live, "record_calls", _refuse_billing(RuntimeError("database is locked"))
    )

    try:
        result = solver(_match_sample(solver.inventory))
    finally:
        database.dispose()

    failure = result["metadata"]["billing_failure"]
    assert failure["origin"] == "eval.match_live"
    assert failure["error_type"] == "RuntimeError"
    assert failure["unbilled_dollars"] == result["metadata"]["actual_dollars"]


def test_a_billed_sample_carries_no_billing_failure(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Billing that succeeded leaves the sample's provenance as it was."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    solver = _paid_match_solver(database, workspace_id, monkeypatch)

    try:
        result = solver(_match_sample(solver.inventory))
        with database.session() as session:
            calls = list(session.scalars(select(ModelCall)))
    finally:
        database.dispose()

    assert len(calls) == 1
    assert "billing_failure" not in result["metadata"]


def test_an_interrupt_during_billing_still_stops_the_run(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containing a ledger failure must not contain the operator's interrupt."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    solver = _paid_match_solver(database, workspace_id, monkeypatch)
    monkeypatch.setattr(clearscout_live, "record_calls", _refuse_billing(KeyboardInterrupt()))

    try:
        with pytest.raises(KeyboardInterrupt):
            solver(_match_sample(solver.inventory))
    finally:
        database.dispose()


def test_a_relevance_sample_survives_a_ledger_write_that_failed(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every paid solver keeps its answer, so the relevance family reports its spend too."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()
    monkeypatch.setattr(relevance_live, "credential_for", lambda provider_name: "token")
    monkeypatch.setattr(
        relevance_live, "build_live_client", lambda *args, **kwargs: _paid_relevance_client(kwargs)
    )
    monkeypatch.setattr(
        clearscout_live, "record_calls", _refuse_billing(RuntimeError("database is locked"))
    )
    solver = relevance_live._RelevanceLiveSolver(
        run_metadata={"provider": "openai"},
        arms=relevance_live.declared_arms(),
        provider_name=None,  # type: ignore[arg-type]
        settings=Settings(provider=ProviderName.OPENAI),
        ledger=Ledger(),
        database=database,
        workspace_id=workspace_id,
    )

    try:
        result = solver(_relevance_sample())
    finally:
        database.dispose()

    assert result["metadata"]["measurements"]
    assert result["metadata"]["billing_failure"]["origin"] == "eval.relevance_review_live"


def test_billing_nothing_writes_nothing(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sample whose call was served from cache leaves the ledger alone."""
    _workspace(durable_database_url, ["only"])
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    database, workspace_id = clearscout_live.billing_target()

    _solver(database, workspace_id)._bill(Ledger())

    try:
        with database.session() as session:
            assert list(session.scalars(select(ModelCall))) == []
    finally:
        database.dispose()
