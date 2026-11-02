"""One runtime storage decision shared by every adapter and service."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tailorcv.application.workspaces import StagedWorkspaceLayout, WorkspaceService
from tailorcv.bootstrap.cli import app
from tailorcv.bootstrap.setup import run_application_setup
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage, WorkspaceRootPolicy
from tailorcv.infrastructure.persistence.database import sqlite_path
from tailorcv.shared.errors import ConfigError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedPrompter


def test_custom_sqlite_keeps_every_implicit_root_beside_the_database(tmp_path: Path) -> None:
    database = tmp_path / "portable" / "state.sqlite3"

    storage = RuntimeStorage.resolve(f"sqlite+pysqlite:///{database}")

    assert sqlite_path(storage.database_url) == database.resolve()
    assert storage.data_root == database.parent.resolve()
    assert storage.artifact_root == database.parent.resolve() / "artifacts"
    assert storage.workspace_root("search") == database.parent.resolve() / "workspaces" / "search"
    assert storage.workspace_policy is WorkspaceRootPolicy.DATA_ROOT


def test_data_root_is_a_coherent_zero_configuration_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAILORCV_DATABASE_URL", raising=False)
    root = tmp_path / "all-data"

    storage = RuntimeStorage.resolve(data_root=root)

    assert storage.database_url == f"sqlite+pysqlite:///{root.resolve() / 'tailorcv.sqlite3'}"
    assert storage.artifact_root == root.resolve() / "artifacts"
    assert storage.workspaces_root == root.resolve() / "workspaces"


def test_serve_creates_api_workspaces_under_an_explicit_workspaces_root(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    served: list[FastAPI] = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **_: served.append(application))
    workspaces = tmp_path / "elsewhere"

    result = CliRunner().invoke(
        app,
        [
            "serve",
            "--database-url",
            durable_database_url,
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--workspaces-root",
            str(workspaces),
        ],
    )

    assert result.exit_code == 0, result.output
    with TestClient(served[0], base_url="http://localhost") as client:
        created = client.post("/api/v1/workspaces", json={"slug": "moved", "name": "Moved"})

    assert created.status_code == 201, created.text
    assert Path(created.json()["root_path"]) == workspaces.resolve() / "moved"


def test_init_creates_its_workspace_under_an_explicit_workspaces_root(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAILORCV_DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda service, name: None)
    workspaces = tmp_path / "elsewhere"

    result = CliRunner().invoke(
        app,
        [
            "init",
            "--cv",
            str(master_cv),
            "--slug",
            "moved",
            "--data-root",
            str(tmp_path / "data"),
            "--workspaces-root",
            str(workspaces),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (workspaces / "moved" / "master-cv" / "CV_Master.md").is_file()
    assert not (tmp_path / "data" / "workspaces").exists()


def test_non_sqlite_requires_an_explicit_local_artifact_boundary(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no local filesystem boundary"):
        RuntimeStorage.resolve("postgresql://localhost/tailorcv")

    storage = RuntimeStorage.resolve(
        "postgresql://localhost/tailorcv", artifact_root=tmp_path / "artifacts"
    )
    assert sqlite_path(storage.database_url) is None
    assert storage.artifact_root == (tmp_path / "artifacts").resolve()


def test_custom_database_setup_never_touches_the_default_documents_tree(
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake-home"
    default_documents = fake_home / "Documents"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(
        "tailorcv.infrastructure.persistence.database.user_documents_path",
        lambda: default_documents,
    )
    monkeypatch.delenv("TAILORCV_DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda service, name: None)
    custom = tmp_path / "portable" / "tailorcv.sqlite3"

    result = run_application_setup(
        ScriptedPrompter(interactive=False),
        source=master_cv,
        workspace_name="Portable",
        slug="portable",
        database_url=f"sqlite+pysqlite:///{custom}",
    )

    assert result.workspace_root == custom.parent / "workspaces" / "portable"
    assert (custom.parent / "artifacts").is_dir()
    assert not (default_documents / "TailorCV").exists()


def test_database_commands_share_data_root_without_touching_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake-home"
    default_documents = fake_home / "Documents"
    data_root = tmp_path / "portable-data"
    backup = tmp_path / "backups" / "portable.sqlite3"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(
        "tailorcv.infrastructure.persistence.database.user_documents_path",
        lambda: default_documents,
    )
    monkeypatch.delenv("TAILORCV_DATABASE_URL", raising=False)
    cli = CliRunner()

    upgraded = cli.invoke(app, ["db", "upgrade", "--data-root", str(data_root)])
    status = cli.invoke(app, ["db", "status", "--data-root", str(data_root)])
    backed_up = cli.invoke(
        app,
        ["db", "backup", str(backup), "--data-root", str(data_root)],
    )

    assert upgraded.exit_code == 0, upgraded.output
    assert status.exit_code == 0 and "Schema current: yes" in status.output
    assert backed_up.exit_code == 0, backed_up.output
    assert (data_root / "tailorcv.sqlite3").is_file()
    assert backup.is_file()
    assert not (default_documents / "TailorCV").exists()


def test_database_status_reports_missing_tables_at_the_current_revision(tmp_path: Path) -> None:
    data_root = tmp_path / "pre-release-data"
    database = data_root / "tailorcv.sqlite3"
    cli = CliRunner()
    upgraded = cli.invoke(app, ["db", "upgrade", "--data-root", str(data_root)])
    assert upgraded.exit_code == 0, upgraded.output
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE worker_presence")

    result = cli.invoke(app, ["db", "status", "--data-root", str(data_root)])

    assert result.exit_code == 0, result.output
    assert "Schema current: no" in result.output
    assert "Missing tables: worker_presence" in result.output

    repeated_upgrade = cli.invoke(app, ["db", "upgrade", "--data-root", str(data_root)])

    assert repeated_upgrade.exit_code == 2
    assert "missing expected schema objects: worker_presence" in repeated_upgrade.output


def test_database_status_reports_missing_columns_at_the_current_revision(tmp_path: Path) -> None:
    data_root = tmp_path / "pre-release-data"
    database = data_root / "tailorcv.sqlite3"
    cli = CliRunner()
    upgraded = cli.invoke(app, ["db", "upgrade", "--data-root", str(data_root)])
    assert upgraded.exit_code == 0, upgraded.output
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE posting_requirements DROP COLUMN gate_quote")

    result = cli.invoke(app, ["db", "status", "--data-root", str(data_root)])

    assert result.exit_code == 0, result.output
    assert "Schema current: no" in result.output
    assert "Missing columns: posting_requirements.gate_quote" in result.output

    repeated_upgrade = cli.invoke(app, ["db", "upgrade", "--data-root", str(data_root)])

    assert repeated_upgrade.exit_code == 2
    assert "missing expected schema objects: posting_requirements.gate_quote" in (
        repeated_upgrade.output
    )


def test_database_status_reports_changed_constraints_at_the_current_revision(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "pre-release-data"
    database = data_root / "tailorcv.sqlite3"
    cli = CliRunner()
    upgraded = cli.invoke(app, ["db", "upgrade", "--data-root", str(data_root)])
    assert upgraded.exit_code == 0, upgraded.output
    with closing(sqlite3.connect(database)) as connection:
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, \"'review'\", \"'priority_review'\") "
            "WHERE type = 'table' AND name = 'relevance_reviews'"
        )
        connection.execute("PRAGMA writable_schema = OFF")
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.commit()

    result = cli.invoke(app, ["db", "status", "--data-root", str(data_root)])

    assert result.exit_code == 0, result.output
    assert "Schema current: no" in result.output
    assert (
        "Changed constraints: relevance_reviews.ck_relevance_review_recommendation" in result.output
    )


def test_workspace_database_failure_discards_private_directory_stage(
    durable_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspaces" / "faulted"

    def fail_flush(self: Session, objects=None) -> None:
        raise RuntimeError("synthetic workspace database failure")

    monkeypatch.setattr(Session, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="synthetic workspace database failure"):
        WorkspaceService(durable_database, runtime_storage(durable_database)).create(
            slug="faulted", name="Faulted", root_path=str(root)
        )

    assert not root.exists()
    assert not list(root.parent.glob(".faulted.tailorcv-stage-*"))


def test_workspace_stage_is_reconciled_by_idempotent_retry(
    durable_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspaces" / "recoverable"
    original = StagedWorkspaceLayout.finalize

    def fail_promotion(self: StagedWorkspaceLayout):
        raise OSError("synthetic workspace promotion failure")

    monkeypatch.setattr(StagedWorkspaceLayout, "finalize", fail_promotion)
    with pytest.raises(OSError, match="synthetic workspace promotion failure"):
        WorkspaceService(durable_database, runtime_storage(durable_database)).create(
            slug="recoverable", name="Recoverable", root_path=str(root)
        )
    assert not root.exists()
    assert list(root.parent.glob(".recoverable.tailorcv-stage-*"))

    monkeypatch.setattr(StagedWorkspaceLayout, "finalize", original)
    recovered = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="recoverable", name="Recoverable", root_path=str(root)
    )
    assert recovered.root_path == str(root)
    assert root.is_dir()
