"""The live vision smoke sends a rendered screenshot through production's vision reader."""

from __future__ import annotations

import json
import struct
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from evals.inspect.scorers.vision_transcript import token_recall
from evals.inspect.solvers import clearscout_live, vision_live
from evals.support.vision_render import source_text

from clearscout.application.master_cv.sources import LIGATURES
from clearscout.infrastructure.persistence.database import Database

DATASET = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "vision_cases.jsonl"


def _png(width: int = 1000, height: int = 1400) -> bytes:
    """Return a PNG signature and IHDR chunk stating one size."""
    header = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + header + b"\x00\x00\x00\x00"


def _sample(sample_id: str) -> dict[str, Any]:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    return next(row for row in rows if row["id"] == sample_id)


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str, sample_id: str, text: str
) -> tuple[dict[str, Any], Any, list[str]]:
    """Run the vision solver with a fake renderer and a provider that transcribes ``text``."""
    from evals.support import client as eval_client

    from clearscout.infrastructure.persistence.models.workspaces import Workspace
    from tests.support.doubles import ScriptedProvider

    database = Database(database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "2")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    rendered: list[str] = []
    provider = ScriptedProvider(script={"VisionTranscript": lambda _schema: {"text": text}})
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider_name, key: provider)

    def render(source: str) -> bytes:
        rendered.append(source)
        return _png()

    solver = vision_live.vision_live_solver(DATASET, render=render)
    try:
        output = solver(_sample(sample_id))
    finally:
        solver.database.dispose()
    return output, provider, rendered


@pytest.mark.parametrize("sample_id", ["vision-cv-academic", "vision-posting"])
def test_a_faithful_transcript_of_each_rendered_source_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str, sample_id: str
) -> None:
    source = source_text(_sample(sample_id)["input"]["source"])
    output, provider, rendered = _run(
        tmp_path, monkeypatch, durable_database_url, sample_id, source
    )

    assert rendered == [source]
    [(_, plan)] = provider.calls
    assert [image.data for image in plan.images] == [_png()]
    score = token_recall()(_sample(sample_id), output)
    assert score["value"] is True, score["explanation"]
    assert score["recall"] == 1.0


def test_a_cv_transcript_that_loses_half_the_page_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    sample_id = "vision-cv-academic"
    lines = source_text(_sample(sample_id)["input"]["source"]).splitlines()
    kept = "\n".join(lines[: len(lines) // 2])
    output, _, _ = _run(tmp_path, monkeypatch, durable_database_url, sample_id, kept)

    score = token_recall()(_sample(sample_id), output)
    assert score["value"] is False
    assert score["recall"] < 0.95
    assert score["metadata"]["missing"]


def test_a_transcript_with_every_word_on_too_few_lines_fails() -> None:
    sample = _sample("vision-cv-academic")
    words = source_text(sample["input"]["source"]).translate(LIGATURES).split()
    output = {"transcript": [" ".join(words)], "line_count": 1}

    score = token_recall()(sample, output)
    assert score["value"] is False
    assert score["recall"] == 1.0


def test_a_vision_row_without_a_recall_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="min_recall"):
        token_recall().validate_sample({"input": {"source": "cv:academic_pdf"}, "expect": {}})


def test_the_live_task_passes_the_smoke_stage_before_its_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The smoke call scores what the run will score, and is reused as the first trial."""
    from functools import partial

    from evals.inspect import runner
    from evals.inspect.tasks import vision_live_task

    first = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    source = source_text(first["input"]["source"])
    _, provider, _ = _run(tmp_path, monkeypatch, durable_database_url, first["id"], source)
    provider.calls.clear()
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "1")
    monkeypatch.setenv("CLEARSCOUT_EVAL_TRIALS", "2")

    rendered: list[str] = []

    def render(text: str) -> bytes:
        rendered.append(text)
        return _png()

    monkeypatch.setattr(
        vision_live_task,
        "vision_live_solver",
        partial(vision_live.vision_live_solver, render=render),
    )

    log = runner.run_task(
        vision_live_task.__name__,
        log_dir=tmp_path,
        limit=1,
        split="development",
        trials=2,
        smoke_approved_usd=Decimal(1),
    )

    assert f"smoke: ok case={first['id']}" in capsys.readouterr().out
    assert rendered == [source, source]
    assert len(provider.calls) == 2
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[1:]]
    assert [row["score"]["value"] for row in rows] == [True, True]


def test_the_vision_scorer_declares_what_its_smoke_reads() -> None:
    assert token_recall().required_fields == ("transcript", "line_count")
