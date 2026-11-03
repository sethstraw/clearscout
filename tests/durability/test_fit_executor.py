"""The paid executor measures the production screening path, offline.

Nothing here contacts a provider. The point is that the executor makes no
requests of its own: it drives the production pipeline, reports what each stage
billed, and labels how the result differs from the human adjudication.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
from evals.dataset import (
    AbstainedScore,
    ComponentScore,
    FitAnnotation,
    FitScores,
    HardGateVerdict,
    Split,
)
from evals.fit_executor import FitExecutor
from evals.semantic import DefectClass

from clearscout.application.config import ProviderName, Settings
from clearscout.domain.records import EvaluationDecision
from clearscout.shared.costs import ServiceTier
from tests.support.fake_provider import FakeProviderAPI
from tests.support.payloads import independent_script
from tests.support.provider_clients import provider_for_http
from tests.support.suite import POSTING

NOT_STATED = AbstainedScore(state="not_stated", reason="The posting states no comparable pay.")


def _annotation(compensation: ComponentScore) -> FitAnnotation:
    return FitAnnotation(
        annotation_id="globex-staff-data-platform-engineer",
        task="fit",
        split=Split.DEVELOPMENT,
        retrieved_on=date(2026, 8, 1),
        adjudicated_on=date(2026, 8, 2),
        artifact_ref="globex.txt",
        sanitized_content_sha256="a" * 64,
        rubric_version="ledger-v1",
        sanitized=True,
        privacy_review_passed=True,
        redistribution_approved=True,
        hard_gate=HardGateVerdict.UNKNOWN,
        decision=EvaluationDecision.INVESTIGATE,
        scores=FitScores(
            role_fit=70,
            compensation=compensation,
            eligibility=AbstainedScore(
                state="not_stated", reason="The posting location is not stated."
            ),
            evidence=70,
            opportunity=60,
        ),
    )


@pytest.fixture
def api() -> FakeProviderAPI:
    return FakeProviderAPI(ProviderName.OPENAI.value, independent_script())


@pytest.fixture
def executor(
    api: FakeProviderAPI, master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FitExecutor:
    http = httpx.Client(transport=httpx.MockTransport(api))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider_for_http(ProviderName.OPENAI, http, max_retries=0),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "globex.txt").write_text(POSTING, encoding="utf-8")
    return FitExecutor(
        artifacts=artifacts,
        master_cv=master_cv,
        # Caching off, so every case reaches the fake and what was sent is readable.
        settings=Settings(
            cv=master_cv,
            output_dir=tmp_path / "runs",
            provider=ProviderName.OPENAI,
            cache=False,
        ),
    )


@pytest.fixture
def flex_executor(executor: FitExecutor) -> FitExecutor:
    executor.service_tier = ServiceTier.FLEX
    return executor


@pytest.mark.transport
def test_a_run_given_the_standard_tier_asks_for_no_discount(
    executor: FitExecutor, api: FakeProviderAPI
) -> None:
    executor.service_tier = ServiceTier.STANDARD
    executor.run(_annotation(80), "baseline")

    assert api.requests, "the screening path sent nothing"
    assert all(request.get("service_tier") in (None, "default") for request in api.requests)


@pytest.mark.transport
def test_the_default_executor_asks_for_the_unattended_tier(
    executor: FitExecutor, api: FakeProviderAPI
) -> None:
    executor.run(_annotation(80), "baseline")

    assert api.requests, "the screening path sent nothing"
    assert {request.get("service_tier") for request in api.requests} == {"flex"}


@pytest.mark.transport
def test_a_stage_measurement_says_which_tier_served_the_call(
    flex_executor: FitExecutor,
) -> None:
    result = flex_executor.run(_annotation(80), "baseline")

    assert {stage.service_tier for stage in result.stages} == {"flex"}


@pytest.mark.transport
def test_the_executor_reports_what_each_production_stage_billed(executor: FitExecutor) -> None:
    result = executor.run(_annotation(80), "baseline")

    assert result.annotation_id == "globex-staff-data-platform-engineer"
    assert result.candidate == "baseline"
    assert {stage.stage for stage in result.stages} == {"jobspec", "match"}
    for stage in result.stages:
        assert stage.dollars is not None, f"{stage.stage} reported no cost to hold to a ceiling"
        assert stage.model == "gpt-6.1-sol"


@pytest.mark.transport
def test_only_the_disagreeing_component_adds_a_defect(executor: FitExecutor) -> None:
    """This posting states no pay, so a scored label disagrees and an abstaining one does not.

    Asserted as the difference between two runs of the same posting, because the
    other components come from the model and would otherwise decide the count.
    """
    scored = executor.run(_annotation(80), "baseline").defects
    agreeing = executor.run(_annotation(NOT_STATED), "baseline").defects

    missed = DefectClass.MISSED_EVIDENCE
    assert scored.count(missed) == agreeing.count(missed) + 1
    assert DefectClass.HIDDEN_GAP not in agreeing


@pytest.mark.transport
def test_content_is_captured_only_when_the_approval_allows_it(executor: FitExecutor) -> None:
    assert executor.run(_annotation(80), "baseline").content == {}

    executor.capture = True
    captured = executor.run(_annotation(80), "baseline").content

    assert captured["artifact_ref"] == "globex.txt"
    assert "jobspec" in captured and "evidence" in captured
