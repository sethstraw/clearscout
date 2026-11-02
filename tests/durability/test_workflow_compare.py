"""The compare operation of the production workflow runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tailorcv.application.config import load_settings
from tailorcv.application.failures import ValidationError
from tailorcv.application.tailoring.batch.analysis import Recommendation, Verdict
from tailorcv.application.tailoring.stages.jobspec import StoredPostingName
from tailorcv.application.tailoring.workflows import PipelineWorkflowRunner
from tailorcv.domain.records import PostingReadIntent
from tailorcv.shared.costs import Ledger
from tests.support.doubles import FakeBatchAnalysis, FakeBatchScreening

STORED_NAME = StoredPostingName(title="Platform Product Lead", employer="Example Logistics")
OTHER_STORED_NAME = StoredPostingName(title="Engineering Lead", employer="Globex")


def _fake_batch_write(target: Path, _result: Any, _inventory: Any) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    report = target.parent / "Batch_Report.md"
    report.write_text("# Batch\n", encoding="utf-8")
    report.with_name("batch.json").write_text('{"jobs": []}\n', encoding="utf-8")
    return report


def test_comparing_screened_postings_keeps_the_stored_name_order_in_the_batch_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "posting.txt"
    second_source = tmp_path / "posting-two.txt"
    master = tmp_path / "master.md"
    source.write_text("Synthetic posting", encoding="utf-8")
    second_source.write_text("Second synthetic posting", encoding="utf-8")
    master.write_text("# Synthetic Candidate\n", encoding="utf-8")
    settings = load_settings(start=tmp_path)
    batch_names: list[StoredPostingName | None] = []

    def fake_screen_batch(
        requests: Any, *_args: Any, out: Path, **_kwargs: Any
    ) -> tuple[FakeBatchAnalysis, object]:
        out.mkdir(parents=True, exist_ok=True)
        batch_names.extend(request.stored_name for request in requests)
        screenings: list[FakeBatchScreening] = []
        recommendations: dict[str, Recommendation] = {}
        for position, _request in enumerate(requests, 1):
            report = out / f"screen-{position}.md"
            report.write_text(f"# Screen {position}\n", encoding="utf-8")
            key = f"job-{position}"
            screenings.append(FakeBatchScreening(key=key, ledger=Ledger(), report=report))
            recommendations[key] = Recommendation(Verdict.PURSUE, ())
        return FakeBatchAnalysis(
            screenings=tuple(screenings), recommendations=recommendations
        ), object()

    monkeypatch.setattr(
        "tailorcv.application.tailoring.workflows.runner.batch.screen_batch", fake_screen_batch
    )
    monkeypatch.setattr(
        "tailorcv.application.tailoring.workflows.runner.batch.write", _fake_batch_write
    )

    compared = PipelineWorkflowRunner().compare(
        (source, second_source),
        settings,
        master_cv=master,
        output_dir=tmp_path / "batch-output",
        priorities=(2, 1),
        notes=("first", "second"),
        stored_names=(STORED_NAME, OTHER_STORED_NAME),
    )

    assert batch_names == [STORED_NAME, OTHER_STORED_NAME]
    assert compared.report == b"# Batch\n"
    assert compared.record == b'{"jobs": []}\n'
    assert [item.outcome for item in compared.screenings] == ["pursue", "pursue"]


def test_comparing_refuses_a_screening_that_wrote_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "posting.txt"
    master = tmp_path / "master.md"
    source.write_text("Synthetic posting", encoding="utf-8")
    master.write_text("# Synthetic Candidate\n", encoding="utf-8")
    settings = load_settings(start=tmp_path)
    monkeypatch.setattr(
        "tailorcv.application.tailoring.workflows.runner.batch.write", _fake_batch_write
    )
    monkeypatch.setattr(
        "tailorcv.application.tailoring.workflows.runner.batch.screen_batch",
        lambda *_args, **_kwargs: (
            FakeBatchAnalysis(
                screenings=(FakeBatchScreening(key="missing", ledger=Ledger(), report=None),),
                recommendations={"missing": Recommendation(Verdict.SKIP, ())},
            ),
            object(),
        ),
    )

    with pytest.raises(ValidationError, match="did not write a screening"):
        PipelineWorkflowRunner().compare(
            (source,),
            settings,
            master_cv=master,
            output_dir=tmp_path / "batch-missing",
            priorities=(0,),
            notes=("",),
        )


class _StopAfterReadError(Exception):
    pass


def test_comparing_stored_postings_reads_them_as_a_crawl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: a compare of stored postings is no address a person just entered."""
    source = tmp_path / "posting.txt"
    second_source = tmp_path / "posting-two.txt"
    master = tmp_path / "master.md"
    source.write_text("Synthetic posting", encoding="utf-8")
    second_source.write_text("Second synthetic posting", encoding="utf-8")
    master.write_text("# Synthetic Candidate\n", encoding="utf-8")
    seen: list[PostingReadIntent] = []

    def recording_screen(_source: str, *_args: Any, intent: PostingReadIntent, **_kw: Any) -> Any:
        seen.append(intent)
        raise _StopAfterReadError

    monkeypatch.setattr("tailorcv.application.tailoring.pipeline.screen", recording_screen)

    with pytest.raises(_StopAfterReadError):
        PipelineWorkflowRunner().compare(
            (source, second_source),
            load_settings(start=tmp_path),
            master_cv=master,
            output_dir=tmp_path / "batch-crawl",
            priorities=(0, 0),
            notes=("", ""),
        )

    assert seen == [PostingReadIntent.CRAWL]
