"""The live CV import task runs production's import steps and grades them against the corpus."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from evals.inspect.scorers.cv_import import import_fidelity
from evals.inspect.solvers import clearscout_live
from evals.support.cv_formats import SHAPES, correct_extraction, cv_format

from clearscout.application.master_cv.sources import read
from clearscout.infrastructure.persistence.database import Database

DATASET = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "structure_cases.jsonl"


def _sample(shape: str) -> dict[str, Any]:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    return next(row for row in rows if row["input"]["shape"] == shape)


def _faithful(shape: str) -> dict[str, Any]:
    path, expected = cv_format(shape)
    return correct_extraction(read(path), expected).model_dump(mode="json")


def _approve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    payload: dict[str, Any],
    limit: int = 6,
) -> Any:
    """Approve a live structure run whose provider answers ``payload`` and return the provider."""
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
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", str(limit))
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    provider = ScriptedProvider(script={"ImportedCv": lambda _schema: payload})
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider_name, key: provider)
    return provider


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    shape: str,
    answer: Callable[[dict[str, Any]], dict[str, Any]] = lambda payload: payload,
) -> tuple[dict[str, Any], Any]:
    """Run the live structure solver on one shape with a provider that answers ``answer``."""
    from evals.inspect.tasks import structure_live_task

    provider = _approve(tmp_path, monkeypatch, database_url, answer(_faithful(shape)))
    solver = structure_live_task.solver()
    try:
        output = solver(_sample(shape))
    finally:
        solver.database.dispose()
    return output, provider


@pytest.mark.parametrize("shape", SHAPES)
def test_a_faithful_extraction_of_each_corpus_cv_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str, shape: str
) -> None:
    output, provider = _run(tmp_path, monkeypatch, durable_database_url, shape)

    assert [call[0] for call in provider.calls] == ["ConstrainedImportedCv"]
    score = import_fidelity()(_sample(shape), output)
    assert score["value"] is True, score["explanation"]
    assert score["failed"] == []


def test_a_recased_title_fails_by_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    def recase(payload: dict[str, Any]) -> dict[str, Any]:
        title = payload["roles"][0]["title"]
        title["text"] = title["text"].upper()
        return payload

    shape = "functional_md"
    output, _ = _run(tmp_path, monkeypatch, durable_database_url, shape, recase)

    score = import_fidelity()(_sample(shape), output)
    assert score["value"] is False
    assert "role 1 title" in score["failed"]


def test_a_dropped_role_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    def drop_last_role(payload: dict[str, Any]) -> dict[str, Any]:
        payload["roles"] = payload["roles"][:-1]
        return payload

    shape = "linkedin_txt"
    output, _ = _run(tmp_path, monkeypatch, durable_database_url, shape, drop_last_role)

    score = import_fidelity()(_sample(shape), output)
    assert score["value"] is False
    assert "role count" in score["failed"]
    assert "lossless" in score["failed_checks"]


def test_dropping_a_content_line_the_corpus_keeps_fails() -> None:
    shape = "academic_pdf"
    path, expected = cv_format(shape)
    source = read(path)
    output = {
        "findings": [{"check": "lossless", "outcome": "warn", "detail": ""}],
        "extracted": _faithful(shape),
        "dropped": [
            *({"line": 0, "text": text, "reason": "heading"} for text in expected["dropped"]),
            {"line": 1, "text": source.lines[0].text, "reason": "heading"},
        ],
    }

    score = import_fidelity()(_sample(shape), output)
    assert score["value"] is False
    assert score["failed"] == ["dropped"]


def test_the_live_task_passes_the_smoke_stage_before_its_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The smoke call scores what the run will score, and is reused as the first trial."""
    from evals.inspect import runner

    first = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    provider = _approve(
        tmp_path, monkeypatch, durable_database_url, _faithful(first["input"]["shape"]), limit=1
    )
    monkeypatch.setenv("CLEARSCOUT_EVAL_TRIALS", "2")
    task = Path(runner.__file__).parent / "tasks" / "structure_live_task.py"

    log = runner.run_task(
        str(task),
        log_dir=tmp_path,
        limit=1,
        split="development",
        trials=2,
        smoke_approved_usd=Decimal(1),
    )

    assert f"smoke: ok case={first['id']}" in capsys.readouterr().out
    assert len(provider.calls) == 2
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[1:]]
    assert [row["score"]["value"] for row in rows] == [True, True]


def test_the_import_scorer_declares_what_its_smoke_reads() -> None:
    assert import_fidelity().required_fields == ("findings", "extracted", "dropped")


def test_the_structure_dataset_holds_one_row_per_corpus_shape() -> None:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    assert sorted(row["input"]["shape"] for row in rows) == sorted(SHAPES)
    assert {row["split"] for row in rows} == {"development"}
