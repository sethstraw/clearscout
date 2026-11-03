"""The evaluate operation of the production workflow runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clearscout.application.config import load_settings
from clearscout.application.tailoring.batch.analysis import Recommendation, Verdict
from clearscout.application.tailoring.stages.jobspec import StoredPostingName
from clearscout.application.tailoring.workflows import PipelineWorkflowRunner
from clearscout.application.tailoring.workflows._support import _posting_name
from clearscout.domain.search_parameters import Seniority
from clearscout.infrastructure.persistence.database import Database
from clearscout.shared.costs import Ledger
from tests.support.doubles import FakeScreenResult, ScriptedProvider
from tests.support.payloads import script
from tests.support.suite import POSTING, SUITE_KEY_VARIABLE, settings_for


def test_the_evaluation_seniority_cap_reads_the_stored_title_rather_than_the_extracted_one(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = script({})
    extracted = answers["JobSpecExtraction"]

    def without_a_rung(schema: Any) -> dict[str, Any]:
        payload: dict[str, Any] = extracted(schema)
        return {**payload, "title": "Product Manager", "seniority": None, "hard_gates": []}

    answers["JobSpecExtraction"] = without_a_rung
    provider = ScriptedProvider(answers)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    base = settings_for(master_cv, tmp_path)
    band = base.job_search.model_copy(
        update={"seniority": Seniority(minimum="mid", maximum="senior")}
    )
    settings = base.model_copy(update={"cache": False, "job_search": band})
    source = tmp_path / "posting.txt"
    source.write_text(POSTING, encoding="utf-8")
    runner = PipelineWorkflowRunner()

    extracted_only = runner.evaluate(source, settings, master_cv=master_cv)
    stored = runner.evaluate(
        source,
        settings,
        master_cv=master_cv,
        stored_name=StoredPostingName(title="Director of Data Products", employer="Globex"),
    )

    assert extracted_only.outcome == Verdict.PURSUE.value
    assert stored.outcome == Verdict.CAUTIOUS.value
    assert any(
        "Director of Data Products" in reason and "above the seniority band" in reason
        for reason in stored.reasons
    )


@pytest.mark.parametrize(("fresh", "asked"), [(False, set()), (True, {"jobspec", "match"})])
def test_only_a_fresh_evaluation_asks_the_provider_past_a_warm_response_cache(
    master_cv: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh: bool,
    asked: set[str],
) -> None:
    provider = ScriptedProvider(script({}))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.cache.user_cache_dir",
        lambda name: str(tmp_path / "cache"),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    settings = settings_for(master_cv, tmp_path).model_copy(update={"cache": True})
    source = tmp_path / "posting.txt"
    source.write_text(POSTING, encoding="utf-8")
    runner = PipelineWorkflowRunner()
    runner.evaluate(source, settings, master_cv=master_cv)
    assert provider.calls
    provider.calls.clear()

    runner.evaluate(source, settings, master_cv=master_cv, fresh=fresh)

    assert {str(plan.stage) for _, plan in provider.calls} == asked


LEVER_POSTING = "https://jobs.lever.co/northwindhealth/1a2b3c"


@pytest.mark.parametrize(
    ("employer", "body", "vouched", "pending_discovery_id", "carried"),
    [
        (
            "Northwindhealth",
            "Northwind Health is hiring a platform lead.",
            True,
            None,
            StoredPostingName(title="Platform Lead", employer="Northwind Health"),
        ),
        (
            "Northwindhealth",
            "Our team is hiring a platform lead.",
            True,
            None,
            StoredPostingName(
                title="Platform Lead", employer="Northwindhealth", employer_from_address_only=True
            ),
        ),
        (
            "Northwindhealth",
            "We improve northwind health outcomes for every clinic.",
            True,
            None,
            StoredPostingName(
                title="Platform Lead", employer="Northwindhealth", employer_from_address_only=True
            ),
        ),
        (
            "Northwind Health",
            "Our team is hiring a platform lead.",
            True,
            None,
            StoredPostingName(title="Platform Lead", employer="Northwind Health"),
        ),
        (
            "Northwindhealth",
            "Northwind Health is hiring a platform lead.",
            False,
            "pending-northwind",
            StoredPostingName(title="Platform Lead", employer="Northwind Health"),
        ),
        (
            "Northwindhealth",
            "Our team is hiring a platform lead.",
            False,
            "pending-northwind",
            StoredPostingName(
                title="Platform Lead", employer="Northwindhealth", employer_from_address_only=True
            ),
        ),
    ],
)
def test_an_employer_read_only_off_the_board_address_is_respelled_or_flagged(
    durable_database: Database,
    employer: str,
    body: str,
    vouched: bool,
    pending_discovery_id: str | None,
    carried: StoredPostingName,
) -> None:
    """A slug name yields to the posting's own spelling, else says it is only the address."""
    with durable_database.session() as session:
        name = _posting_name(
            session,
            workspace_id="workspace-northwind",
            title="Platform Lead",
            employer=employer,
            body=body.encode("utf-8"),
            vouched=vouched,
            pending_discovery_id=pending_discovery_id,
            posting_urls=(LEVER_POSTING, LEVER_POSTING),
        )

    assert name == carried


def test_evaluating_a_screened_posting_normalizes_the_outcome_reasons_and_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "posting.txt"
    master = tmp_path / "master.md"
    source.write_text("Synthetic posting", encoding="utf-8")
    master.write_text("# Synthetic Candidate\n", encoding="utf-8")
    settings = load_settings(start=tmp_path)
    runner = PipelineWorkflowRunner()
    ledger = Ledger()

    def fake_screen(*_args: Any, out: Path | None = None, **_kwargs: Any) -> FakeScreenResult:
        report = None
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            report = out / "Screening_Report.md"
            report.write_text("# Screening\n", encoding="utf-8")
        return FakeScreenResult(
            spec=object(),
            evidence=object(),
            inventory=object(),
            ledger=ledger,
            jd=object(),
            reading=object(),
            report=report,
        )

    monkeypatch.setattr(
        "clearscout.application.tailoring.workflows.runner.pipeline.screen", fake_screen
    )
    monkeypatch.setattr(
        "clearscout.application.tailoring.workflows.runner.batch.recommend",
        lambda _spec, _evidence, **_: Recommendation(Verdict.PURSUE, ("direct evidence",)),
    )

    evaluated = runner.evaluate(source, settings, master_cv=master)

    assert evaluated.outcome == "pursue"
    assert evaluated.reasons == ("direct evidence",)
    assert evaluated.prompt_fingerprint
