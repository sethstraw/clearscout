"""Cost estimator refuses below the sample floor and averages what it has."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from evals.inspect.cost_estimate import MIN_SAMPLES, estimate, main
from sqlalchemy import select

from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.migration import upgrade
from tailorcv.infrastructure.persistence.models.tailoring import CostRecord, ModelCall
from tailorcv.infrastructure.persistence.models.workspaces import Workspace


def _seed(
    url: str,
    amounts: list[str],
    stage: str = "match",
    model: str = "gpt-5.6-terra",
    service_tier: str | None = None,
    effort: str | None = None,
    origin: str = "workflow",
) -> None:
    database = Database(url)
    now = datetime.now(UTC)
    try:
        with database.session() as session:
            workspace = session.scalars(select(Workspace)).one_or_none()
            if workspace is None:
                workspace = Workspace(slug="ws", name="Workspace")
                session.add(workspace)
                session.flush()
            for amount in amounts:
                call = ModelCall(
                    workspace_id=workspace.id,
                    stage=stage,
                    provider="openai",
                    requested_model=model,
                    service_tier=service_tier,
                    effort=effort,
                    origin=origin,
                    input_tokens=1200,
                    output_tokens=300,
                    validated=True,
                    cache_hit=False,
                    created_at=now,
                )
                session.add(call)
                session.flush()
                session.add(
                    CostRecord(
                        model_call_id=call.id,
                        amount=Decimal(amount),
                        currency="USD",
                        pricing_source="configured",
                    )
                )
    finally:
        database.dispose()


@pytest.mark.parametrize("samples", [0, 1, MIN_SAMPLES - 1])
def test_refuses_below_the_sample_floor(tmp_path: Path, samples: int) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    if samples:
        _seed(url, [f"0.{index:02d}" for index in range(1, samples + 1)])

    exit_code = estimate(tmp_path, stage="match", model="gpt-5.6-terra", limit=10)

    assert exit_code == 2


def test_prints_a_grounded_estimate_when_enough_history_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.10", "0.20", "0.30", "0.40", "0.50"])

    exit_code = estimate(tmp_path, stage="match", model="gpt-5.6-terra", limit=10, sample_size=20)

    stdout = capsys.readouterr().out
    assert exit_code == 0
    # Five samples averaging thirty cents each times ten equals three dollars.
    assert "N=5" in stdout
    assert "$3.00" in stdout


def test_scales_the_estimate_by_the_trial_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Repeating every sample pays for every repeat, so the estimate multiplies."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.10", "0.20", "0.30", "0.40", "0.50"])

    exit_code = estimate(
        tmp_path, stage="match", model="gpt-5.6-terra", limit=10, sample_size=20, trials=3
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "--trials=3" in stdout
    # Thirty cents a call, ten samples, three trials each.
    assert "$9.00" in stdout


def test_scales_the_estimate_by_the_arm_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ten trials of four cases across two arms is eighty calls, and the estimate says so."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.10", "0.20", "0.30", "0.40", "0.50"], stage="relevance_review")

    exit_code = estimate(
        tmp_path,
        stage="relevance_review",
        model="gpt-5.6-terra",
        limit=4,
        sample_size=20,
        trials=10,
        arms=2,
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "--limit=4 --trials=10 --arms=2" in stdout
    # Thirty cents a call, four cases, ten trials per arm, two arms.
    assert "$24.00" in stdout


def test_scales_the_estimate_by_the_selected_id_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming four samples costs the same as a limit of four, and the scope says which."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.10", "0.20", "0.30", "0.40", "0.50"], stage="relevance_review")

    exit_code = estimate(
        tmp_path,
        stage="relevance_review",
        model="gpt-5.6-terra",
        sample_size=20,
        trials=10,
        arms=2,
        ids=["case-6", "case-7", "case-13", "case-15"],
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "--ids=case-6,case-7,case-13,case-15 --trials=10 --arms=2" in stdout
    # Thirty cents a call, four named cases, ten trials per arm, two arms.
    assert "$24.00" in stdout


def test_ignores_other_stages(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.99", "0.99", "0.99", "0.99", "0.99"], stage="draft")

    exit_code = estimate(tmp_path, stage="match", model="gpt-5.6-terra", limit=10)

    assert exit_code == 2


def test_ignores_other_models(tmp_path: Path) -> None:
    """A model with no history of its own is not estimated at another model's rate."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.99", "0.99", "0.99", "0.99", "0.99"])

    exit_code = estimate(tmp_path, stage="match", model="gpt-6.1-sol", limit=10)

    assert exit_code == 2


@pytest.mark.parametrize(
    ("named", "expected"),
    [([], "gpt-6.1-sol"), (["--model", "gpt-6-luna"], "gpt-6-luna")],
)
def test_the_command_prices_the_model_the_run_sends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], named: list[str], expected: str
) -> None:
    """Without --model the stage's shipped model for that provider is the one estimated."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(
        url,
        ["0.10", "0.20", "0.30", "0.40", "0.50"],
        stage="draft",
        model=expected,
        service_tier="flex",
        effort="medium",
    )

    exit_code = main(
        [
            "--data-root",
            str(tmp_path),
            "--stage",
            "draft",
            "--provider",
            "openai",
            "--limit",
            "2",
            *named,
        ]
    )

    assert exit_code == 0
    assert f"draft calls on {expected} averaged" in capsys.readouterr().out


def test_the_command_pays_for_every_critic_trial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each judged draft costs one call per critic trial, not one call."""
    monkeypatch.setenv("TAILORCV_CRITIC_TRIALS", "3")
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(
        url,
        ["0.10", "0.20", "0.30", "0.40", "0.50"],
        stage="critic",
        model="gpt-6.1-sol",
        service_tier="flex",
        effort="medium",
    )

    exit_code = main(
        ["--data-root", str(tmp_path), "--stage", "critic", "--provider", "openai", "--limit", "2"]
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    # Thirty cents a call, two drafts, three critic trials each.
    assert "--limit=2 at 3 calls per sample is $1.80" in stdout


def _relevance_task(tmp_path: Path) -> Path:
    """A task over three development rows under two search contexts, and one held-out row."""
    rows = [
        ("rel-a1", "Places this person may work: Calgary", "development"),
        ("rel-b1", "Places this person may work: Halifax", "development"),
        ("rel-a2", "Places this person may work: Calgary", "development"),
        ("rel-h1", "Places this person may work: Regina", "holdout"),
    ]
    dataset = tmp_path / "relevance.jsonl"
    dataset.write_text(
        "".join(
            json.dumps(
                {
                    "id": sample_id,
                    "split": split,
                    "input": {
                        "search_context": context,
                        "posting": {
                            "id": sample_id,
                            "title": "Platform Engineer",
                            "body": "Own it.",
                        },
                    },
                }
            )
            + "\n"
            for sample_id, context, split in rows
        ),
        encoding="utf-8",
    )
    task = tmp_path / "relevance_task.py"
    task.write_text(f"from pathlib import Path\nDATASET_PATH = Path({str(dataset)!r})\n")
    return task


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        (["--limit", "3"], "--limit=3 --trials=2 in 4 batched calls is $1.20"),
        (["--ids", "rel-a1", "rel-a2"], "--trials=2 in 2 batched calls is $0.60"),
    ],
)
def test_a_relevance_estimate_prices_the_batch_calls_the_run_sends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], selection: list[str], expected: str
) -> None:
    """Rows sharing a search context ride one call, so the estimate counts calls, not rows."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(
        url,
        ["0.10", "0.20", "0.30", "0.40", "0.50"],
        stage="relevance_review",
        model="gpt-6-luna",
        service_tier="flex",
        effort="low",
    )

    exit_code = main(
        [
            "--data-root",
            str(tmp_path),
            "--stage",
            "relevance_review",
            "--provider",
            "openai",
            "--model",
            "gpt-6-luna",
            "--effort",
            "low",
            *selection,
            "--trials",
            "2",
            "--task",
            str(_relevance_task(tmp_path)),
            "--split",
            "development",
        ]
    )

    assert exit_code == 0
    assert expected in capsys.readouterr().out


def test_an_openai_estimate_averages_only_the_flex_calls_the_run_will_send(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live OpenAI run is served on flex, so standard-priced history would overstate it."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(
        url, ["0.10"] * 5, stage="draft", model="gpt-6.1-sol", service_tier="flex", effort="medium"
    )
    _seed(
        url,
        ["0.50"] * 5,
        stage="draft",
        model="gpt-6.1-sol",
        service_tier="default",
        effort="medium",
    )

    exit_code = main(
        ["--data-root", str(tmp_path), "--stage", "draft", "--provider", "openai", "--limit", "2"]
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "N=5, flex tier" in stdout
    assert "--limit=2 is $0.20" in stdout


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        ([], "medium effort; estimated cost for --limit=2 is $1.00"),
        (["--effort", "low"], "low effort; estimated cost for --limit=2 is $0.20"),
    ],
)
def test_the_command_averages_only_the_effort_the_run_sends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], named: list[str], expected: str
) -> None:
    """Effort changes what a call bills, so another effort's history would misstate the run."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.10"] * 5, stage="draft", model="gpt-6.1-sol", service_tier="flex", effort="low")
    _seed(
        url, ["0.50"] * 5, stage="draft", model="gpt-6.1-sol", service_tier="flex", effort="medium"
    )

    exit_code = main(
        [
            "--data-root",
            str(tmp_path),
            "--stage",
            "draft",
            "--provider",
            "openai",
            "--limit",
            "2",
            *named,
        ]
    )

    assert exit_code == 0
    assert expected in capsys.readouterr().out


def test_the_estimate_averages_production_calls_not_earlier_eval_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An eval call's shape differs from the production call a stage sends, so it stays out."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.40"] * 5, stage="relevance_review", origin="discovery.relevance_review")
    _seed(url, ["0.01"] * 5, stage="relevance_review", origin="eval.relevance_review_live")

    exit_code = estimate(tmp_path, stage="relevance_review", model="gpt-5.6-terra", limit=10)

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "N=5" in stdout
    assert "$4.00" in stdout


def test_a_standard_estimate_leaves_out_flex_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Standard history is every spelling a provider reports for it, and never flex."""
    url = f"sqlite+pysqlite:///{tmp_path / 'tailorcv.sqlite3'}"
    upgrade(url)
    _seed(url, ["0.40"] * 2)
    _seed(url, ["0.40"] * 2, service_tier="default")
    _seed(url, ["0.40"], service_tier="standard")
    _seed(url, ["0.10"] * 5, service_tier="flex")

    exit_code = estimate(tmp_path, stage="match", model="gpt-5.6-terra", limit=10)

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "N=5, standard tier" in stdout
    assert "$4.00" in stdout
