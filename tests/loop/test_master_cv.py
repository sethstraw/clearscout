from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from clearscout.application import job_search
from clearscout.application.contracts import EvaluationCommit, ScoreEvidenceInput
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.master_cv import MasterCvService
from clearscout.application.master_cv.artifacts import MasterCvArtifacts
from clearscout.application.master_cv.commands import MasterCvVersionCommands
from clearscout.application.master_cv.cv import Entry, EntryKind, parse_cv
from clearscout.application.master_cv.intake import MasterCvIntake
from clearscout.application.master_cv.queries import MasterCvQueries
from clearscout.application.workspaces import (
    WorkspaceResponse,
    WorkspaceService,
    layout_for,
    prepare_layout,
)
from clearscout.domain import (
    EvidenceClassification,
    EvidenceSourceKind,
    EvidenceStrength,
    ScoreComponent,
)
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    JobEvaluation,
    MasterCvEntry,
    MasterCvVersion,
    ModelCall,
)
from clearscout.shared.costs import CallManifest, Ledger, ModelPrice, Usage
from clearscout.shared.errors import AuthError, CvParseError
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.doubles import FakeImportResult
from tests.support.scenarios import NOW, evaluation_commit


def test_default_application_data_is_visible_under_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clearscout.infrastructure.persistence import database as database_module

    monkeypatch.setattr(database_module, "user_documents_path", lambda: tmp_path / "Documents")
    assert database_module.default_data_directory() == tmp_path / "Documents" / "ClearScout"
    assert database_module.default_database_path().name == "clearscout.sqlite3"


def _workspace(database: Database, tmp_path: Path) -> WorkspaceResponse:
    return WorkspaceService(database, runtime_storage(database)).create(
        slug="master-cv",
        name="Master CV",
        root_path=str(tmp_path / "workspace"),
    )


def test_master_cv_service_composes_the_intake_version_artifact_and_query_boundaries(
    durable_database: Database, tmp_path: Path
) -> None:
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )

    assert isinstance(service.intake, MasterCvIntake)
    assert isinstance(service.commands, MasterCvVersionCommands)
    assert isinstance(service.artifact_coordination, MasterCvArtifacts)
    assert isinstance(service.queries, MasterCvQueries)


# One sentence of the synthetic CV, quoted verbatim, so a committed evaluation
# cites the master CV itself rather than a stand-in string.
CITED_QUOTE = "cutting median end to end latency from 40 minutes to 90 seconds"


def _evaluation_citing(
    workspace_id: str, cv_text: str, quote: str, entry_id: str = "cv-1"
) -> EvaluationCommit:
    """A proposal whose candidate evidence quotes the stored master CV at its span."""
    start = cv_text.index(quote)
    return evaluation_commit(
        workspace_id,
        candidate_record={
            "content": cv_text,
            "source_uri": "test://master-cv",
            "effective_at": NOW,
        },
        score_evidence=[
            ScoreEvidenceInput(
                key="candidate-platform",
                source_kind=EvidenceSourceKind.CANDIDATE,
                text=quote,
                char_start=start,
                char_end=start + len(quote),
                components=[ScoreComponent.FIT, ScoreComponent.EVIDENCE],
                classification=EvidenceClassification.DIRECT,
                strength=EvidenceStrength.STRONG,
                requirement_id="REQ-1",
                master_cv_entry_id=entry_id,
            ),
            ScoreEvidenceInput(
                key="posting-location",
                source_kind=EvidenceSourceKind.POSTING,
                text="Canada remote",
                char_start=43,
                char_end=56,
                components=[
                    ScoreComponent.COMPENSATION,
                    ScoreComponent.ELIGIBILITY,
                    ScoreComponent.OPPORTUNITY,
                ],
                classification=EvidenceClassification.DIRECT,
                strength=EvidenceStrength.MODERATE,
                requirement_id="REQ-2",
            ),
        ],
    )


def test_parseable_markdown_is_adopted_without_a_model(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.import_cv",
        lambda *args, **kwargs: pytest.fail("canonical Markdown must not call a provider"),
    )

    result = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    ).import_source(
        workspace_id=str(workspace.id),
        filename="Existing CV.md",
        content=master_cv.read_bytes(),
    )

    canonical = tmp_path / "workspace" / "master-cv" / "CV_Master.md"
    assert result.method == "adopted"
    assert result.entry_count > 0
    assert canonical.read_bytes() == master_cv.read_bytes()
    assert Path(result.source_path).parent.name == "imports"
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MasterCvVersion)) == 1
        assert session.scalar(select(func.count()).select_from(Artifact)) == 1

    replay = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    ).import_source(
        workspace_id=str(workspace.id),
        filename="Existing CV.md",
        content=master_cv.read_bytes(),
    )
    assert replay.master_cv_version_id == result.master_cv_version_id

    Path(result.source_path).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValidationError, match="content-hash"):
        MasterCvService(
            durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
        ).import_source(
            workspace_id=str(workspace.id),
            filename="Existing CV.md",
            content=master_cv.read_bytes(),
        )


def test_an_adopted_version_stores_every_entry_the_parse_found(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace = _workspace(durable_database, tmp_path)

    result = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    ).import_source(
        workspace_id=str(workspace.id),
        filename="Existing CV.md",
        content=master_cv.read_bytes(),
    )

    parsed = parse_cv(master_cv)
    with durable_database.session() as session:
        rows = list(
            session.scalars(
                select(MasterCvEntry)
                .where(MasterCvEntry.master_cv_version_id == result.master_cv_version_id)
                .order_by(MasterCvEntry.position)
            )
        )
        stored = [
            Entry(
                id=row.entry_id,
                kind=EntryKind(row.kind),
                section=row.section,
                text=row.text,
                role_key=row.role_key,
                group=row.group,
                label=row.label,
                details=tuple(row.details),
                order=row.position,
            )
            for row in rows
        ]

    assert len(stored) == len(parsed.entries) == result.entry_count
    assert stored == list(parsed.entries)


def test_re_adopting_an_unchanged_cv_gives_an_older_version_its_entries(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    first = service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )
    with durable_database.session() as session:
        session.query(MasterCvEntry).delete()
        session.commit()

    again = service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )

    assert again.master_cv_version_id == first.master_cv_version_id
    with durable_database.session() as session:
        stored = _keys_by_text(session, first.master_cv_version_id)
    assert len(stored) == first.entry_count


def test_editing_one_bullet_leaves_every_other_identity_alone(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A citation survives a rewording, and the entries beside it do not move."""
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    first = service.import_source(
        workspace_id=str(workspace.id),
        filename="CV.md",
        content=master_cv.read_bytes(),
    )
    original = master_cv.read_text(encoding="utf-8")
    edited = original.replace("cutting median end to end latency", "cutting median latency", 1)
    assert edited != original

    second = service.import_source(
        workspace_id=str(workspace.id),
        filename="CV.md",
        content=edited.encode("utf-8"),
        replace=True,
    )

    with durable_database.session() as session:
        before = _keys_by_text(session, first.master_cv_version_id)
        after = _keys_by_text(session, second.master_cv_version_id)

    assert second.master_cv_version_id != first.master_cv_version_id
    unchanged = set(before) & set(after)
    assert {text: after[text] for text in unchanged} == {text: before[text] for text in unchanged}
    reworded = set(after) - set(before)
    replaced = set(before) - set(after)
    assert len(reworded) == len(replaced) == 1
    assert after[reworded.pop()] == before[replaced.pop()]


def _keys_by_text(session: Any, version_id: str) -> dict[str, str]:
    """Return every stored entry's durable key, keyed by the text it carries."""
    return {
        row.text: row.entry_key
        for row in session.scalars(
            select(MasterCvEntry).where(MasterCvEntry.master_cv_version_id == version_id)
        )
    }


def test_workspace_layout_refuses_broad_roots(durable_database: Database) -> None:
    with pytest.raises(ValidationError, match="dedicated folder"):
        prepare_layout(
            layout_for("unsafe", storage=runtime_storage(durable_database), root=Path.home()),
            private_root=False,
        )


def test_unstructured_source_reuses_the_checked_pipeline(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    called: dict[str, Any] = {}

    def import_cv(source: Path, settings: Any, *, destination: Path, **kwargs: Any) -> Any:
        called.update(source=source, settings=settings, destination=destination, kwargs=kwargs)
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = destination.parent / "Import_Report.md"
        report.write_text("# Checked import\n", encoding="utf-8")
        return FakeImportResult(
            passed=True,
            markdown=master_cv.read_text(encoding="utf-8"),
            report=report,
            ledger=Ledger(),
        )

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.import_cv", import_cv)
    result = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    ).import_source(
        workspace_id=str(workspace.id),
        filename="Resume.docx",
        content=b"synthetic Word package",
    )

    assert result.method == "standardized"
    assert Path(str(result.report_path)).name == "Import_Report.md"
    assert Path(called["source"]).suffix == ".docx"
    assert Path(called["destination"]).parts[-2].startswith("cv-import-")
    assert called["kwargs"]["force"] is True


def test_standardizing_a_cv_files_the_calls_it_was_billed_for(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import reads and structures the document with a model, so it spends.

    The spend reaches the store, or every later window check and every panel
    would be short by it.
    """
    workspace = _workspace(durable_database, tmp_path)
    ledger = Ledger(prices={"m": ModelPrice(input=2.0, output=12.0)})
    ledger.record(CallManifest("structure", "m", Usage(input_tokens=1_000_000)))

    def import_cv(source: Path, settings: Any, *, destination: Path, **kwargs: Any) -> Any:
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = destination.parent / "Import_Report.md"
        report.write_text("# Checked import\n", encoding="utf-8")
        return FakeImportResult(
            passed=True,
            markdown=master_cv.read_text(encoding="utf-8"),
            report=report,
            ledger=ledger,
        )

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.import_cv", import_cv)
    MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    ).import_source(
        workspace_id=str(workspace.id),
        filename="Resume.docx",
        content=b"synthetic Word package",
    )

    with durable_database.session() as session:
        filed = session.execute(select(ModelCall)).scalars().all()
    assert [(call.stage, call.workspace_id) for call in filed] == [("structure", str(workspace.id))]


def test_failed_standardized_parse_leaves_canonical_and_registration_unchanged(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    service.import_source(
        workspace_id=str(workspace.id), filename="first.md", content=master_cv.read_bytes()
    )
    canonical = tmp_path / "workspace" / "master-cv" / "CV_Master.md"
    original = canonical.read_bytes()
    with durable_database.session() as session:
        versions_before = session.scalar(select(func.count()).select_from(MasterCvVersion))
        artifacts_before = session.scalar(select(func.count()).select_from(Artifact))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.import_cv",
        lambda *args, **kwargs: FakeImportResult(
            passed=True,
            markdown="not a CV any more\n",
            report=tmp_path / "Import_Report.md",
            ledger=Ledger(),
        ),
    )

    with pytest.raises(CvParseError):
        service.import_source(
            workspace_id=str(workspace.id),
            filename="replacement.pdf",
            content=b"synthetic PDF",
            replace=True,
        )

    assert canonical.read_bytes() == original
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MasterCvVersion)) == versions_before
        assert session.scalar(select(func.count()).select_from(Artifact)) == artifacts_before


def test_a_different_cv_requires_explicit_replacement(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    service.import_source(
        workspace_id=str(workspace.id), filename="first.md", content=master_cv.read_bytes()
    )
    changed = master_cv.read_text(encoding="utf-8").replace(
        "Recent work centers on streaming ingestion, schema governance,",
        "Recent work centers on batch ingestion, schema governance,",
    )
    with pytest.raises(ConflictError, match="already has a different master CV"):
        service.import_source(
            workspace_id=str(workspace.id),
            filename="changed.md",
            content=changed.encode(),
        )
    replaced = service.import_source(
        workspace_id=str(workspace.id),
        filename="changed.md",
        content=changed.encode(),
        replace=True,
    )
    assert replaced.content_sha256 != ""
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MasterCvVersion)) == 2


def test_failed_database_registration_never_replaces_the_canonical_master_cv(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    service.import_source(
        workspace_id=str(workspace.id), filename="first.md", content=master_cv.read_bytes()
    )
    canonical = tmp_path / "workspace" / "master-cv" / "CV_Master.md"
    original = canonical.read_bytes()
    changed = original.replace(b"streaming ingestion", b"batch ingestion")

    def fail_registration(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic database registration failure")

    monkeypatch.setattr(
        "clearscout.application.master_cv.commands.register_master_cv", fail_registration
    )
    with pytest.raises(RuntimeError, match="synthetic database registration failure"):
        service.import_source(
            workspace_id=str(workspace.id),
            filename="changed.md",
            content=changed,
            replace=True,
        )

    assert canonical.read_bytes() == original
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MasterCvVersion)) == 1


@pytest.mark.parametrize(
    ("workspace_id", "filename", "content", "error"),
    [
        ("missing", "resume.md", b"body", NotFoundError),
        ("present", "../resume.md", b"body", ValidationError),
        ("present", "resume.exe", b"body", ValidationError),
        ("present", "resume.md", b"", ValidationError),
    ],
)
def test_invalid_uploads_are_rejected_before_standardization(
    durable_database: Database,
    tmp_path: Path,
    workspace_id: str,
    filename: str,
    content: bytes,
    error: type[Exception],
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    actual_id = str(workspace.id) if workspace_id == "present" else workspace_id
    with pytest.raises(error):
        MasterCvService(
            durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
        ).import_source(
            workspace_id=actual_id,
            filename=filename,
            content=content,
        )


def test_pipeline_errors_and_failed_checks_never_register_a_version(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.import_cv",
        lambda *args, **kwargs: (_ for _ in ()).throw(AuthError("No provider key.")),
    )
    with pytest.raises(ValidationError, match="No provider key"):
        service.import_source(workspace_id=str(workspace.id), filename="resume.pdf", content=b"pdf")

    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.import_cv",
        lambda *args, **kwargs: FakeImportResult(
            passed=False,
            markdown="# Rejected\n",
            report=tmp_path / "Import_Report.md",
            ledger=Ledger(),
        ),
    )
    with pytest.raises(ValidationError, match="did not pass"):
        service.import_source(
            workspace_id=str(workspace.id), filename="resume.pdf", content=b"other pdf"
        )
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MasterCvVersion)) == 0


def test_master_cv_can_be_read_back_and_compared(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A stored CV reads back, so a committed citation can be checked against it."""
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    first = service.import_source(
        workspace_id=str(workspace.id),
        filename="CV.md",
        content=master_cv.read_bytes(),
    )

    current = service.read_version(str(workspace.id))
    assert current.is_current is True
    assert current.content_sha256 == first.content_sha256
    assert current.content.strip(), "the stored text is readable, not just its hash"

    replaced = master_cv.read_text() + "\n- An added line of evidence.\n"
    second = service.import_source(
        workspace_id=str(workspace.id),
        filename="CV.md",
        content=replaced.encode("utf-8"),
        replace=True,
    )

    versions = service.versions(str(workspace.id))
    assert [item.is_current for item in versions] == [True, False]
    assert versions[0].content_sha256 == second.content_sha256

    # An earlier version stays readable, which is what makes a stranded
    # citation explainable rather than merely wrong.
    earlier = service.read_version(str(workspace.id), str(versions[1].id))
    assert earlier.content_sha256 == first.content_sha256

    diff = service.diff_versions(str(workspace.id), str(versions[1].id), str(versions[0].id))
    assert "+- An added line of evidence." in diff


def test_an_edit_appends_a_version_and_leaves_the_earlier_one_readable(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Editing in place is the import path with supplied text, not a second writer."""
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    first = service.import_source(
        workspace_id=str(workspace.id),
        filename="CV.md",
        content=master_cv.read_bytes(),
    )

    edited = master_cv.read_bytes().decode("utf-8") + "\n- An edited line of evidence.\n"
    saved = service.save_edit(str(workspace.id), edited)

    assert saved.created is True
    assert saved.previous_master_cv_version_id == first.master_cv_version_id
    assert saved.method == "adopted"
    versions = service.versions(str(workspace.id))
    assert len(versions) == 2
    assert service.read_version(str(workspace.id)).content == edited
    earlier = service.read_version(str(workspace.id), first.master_cv_version_id)
    assert earlier.content_sha256 == first.content_sha256
    assert "An edited line of evidence." not in earlier.content

    # Standardization costs money, so text that no longer meets the contract is
    # refused here rather than sent to a model.
    with pytest.raises(ValidationError, match="no longer parses"):
        service.save_edit(str(workspace.id), "not a CV any more\n")


UNDATED_EDIT = "# JANE Q. EXAMPLE\n\n## Summary\n\nJane Example builds data platforms.\n"


@pytest.mark.parametrize("filename", ["Resume.docx", "notes.txt"])
def test_an_unconfirmed_import_the_model_must_re_read_is_refused_without_a_model(
    durable_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.import_cv",
        lambda *args, **kwargs: pytest.fail("an unconfirmed import must not call a provider"),
    )

    with pytest.raises(ConflictError, match="re-read"):
        service.import_source(
            workspace_id=str(workspace.id),
            filename=filename,
            content=UNDATED_EDIT.encode("utf-8"),
            confirm_reread=False,
        )

    assert service.versions(str(workspace.id)) == []


def test_an_unconfirmed_edit_that_needs_a_re_read_is_refused_without_a_model(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.import_cv",
        lambda *args, **kwargs: pytest.fail("saving an edit must not call a provider"),
    )

    with pytest.raises(ConflictError, match="no dated role"):
        service.save_edit(str(workspace.id), UNDATED_EDIT)

    assert len(service.versions(str(workspace.id))) == 1


def test_a_confirmed_re_read_standardizes_the_edit_and_appends_a_version(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    first = service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )
    read: list[str] = []

    def import_cv(source: Path, settings: Any, *, destination: Path, **kwargs: Any) -> Any:
        read.append(source.read_text(encoding="utf-8"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = destination.parent / "Import_Report.md"
        report.write_text("# Checked import\n", encoding="utf-8")
        return FakeImportResult(
            passed=True,
            markdown=master_cv.read_text(encoding="utf-8") + "\n- A re-read line.\n",
            report=report,
        )

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.import_cv", import_cv)

    saved = service.save_edit(str(workspace.id), UNDATED_EDIT, confirm_reread=True)

    assert read == [UNDATED_EDIT]
    assert saved.method == "standardized"
    assert saved.created is True
    assert saved.previous_master_cv_version_id == first.master_cv_version_id
    assert len(service.versions(str(workspace.id))) == 2


def test_impact_names_why_an_edit_needs_a_re_read(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )

    undated = service.impact(str(workspace.id), UNDATED_EDIT)
    canonical = service.impact(str(workspace.id), master_cv.read_text(encoding="utf-8"))

    assert undated.reread_reason == "it has no dated role"
    assert canonical.reread_reason is None


def test_the_re_read_estimate_averages_past_re_reads_and_stays_blank_without_one(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    before = service.reread_estimate(str(workspace.id))
    ledger = Ledger(prices={"m": ModelPrice(input=2.0, output=12.0)})
    ledger.record(CallManifest("ingest", "m", Usage(input_tokens=500_000)))
    ledger.record(CallManifest("structure", "m", Usage(input_tokens=1_000_000)))

    def import_cv(source: Path, settings: Any, *, destination: Path, **kwargs: Any) -> Any:
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = destination.parent / "Import_Report.md"
        report.write_text("# Checked import\n", encoding="utf-8")
        return FakeImportResult(
            passed=True,
            markdown=master_cv.read_text(encoding="utf-8"),
            report=report,
            ledger=ledger,
        )

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.import_cv", import_cv)
    service.import_source(
        workspace_id=str(workspace.id), filename="Resume.docx", content=b"synthetic Word package"
    )
    after = service.reread_estimate(str(workspace.id))

    assert (before.past_rereads, before.average_usd) == (0, None)
    assert after.past_rereads == 1
    assert after.average_usd == pytest.approx(3.0)


def test_saving_identical_content_writes_no_new_version(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    first = service.import_source(
        workspace_id=str(workspace.id),
        filename="CV.md",
        content=master_cv.read_bytes(),
    )

    saved = service.save_edit(str(workspace.id), master_cv.read_bytes().decode("utf-8"))

    assert saved.created is False
    assert saved.master_cv_version_id == first.master_cv_version_id
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MasterCvVersion)) == 1


def test_a_committed_evaluation_still_reads_the_version_it_cited(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """An edit cannot reach a committed evaluation, which pins its own version."""
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    cv_text = master_cv.read_bytes().decode("utf-8")
    first = service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )
    job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        _evaluation_citing(str(workspace.id), cv_text, CITED_QUOTE),
    )

    service.save_edit(str(workspace.id), cv_text.replace("90 seconds", "30 seconds"))

    with durable_database.session() as session:
        pinned = session.scalar(select(JobEvaluation.master_cv_version_id))
    assert pinned == first.master_cv_version_id
    cited = service.read_version(str(workspace.id), str(pinned))
    assert cited.content == cv_text
    assert cited.is_current is False


def test_impact_separates_a_reworded_entry_from_one_the_edit_removes(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    cv_text = master_cv.read_bytes().decode("utf-8")
    service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )
    cited = next(entry for entry in parse_cv(master_cv).entries if CITED_QUOTE in entry.text)
    job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        _evaluation_citing(str(workspace.id), cv_text, cited.text, entry_id=cited.id),
    )

    reworded = service.impact(
        str(workspace.id), cv_text.replace("Rebuilt the ingestion tier", "Rebuilt ingestion")
    )
    removed = service.impact(str(workspace.id), cv_text.replace(f"- {cited.text}\n", ""))

    assert reworded.citations[0].entry_carried is True
    assert reworded.orphaned_count == 0
    assert removed.citations[0].entry_carried is False
    assert removed.orphaned_count == 1


def test_impact_reports_changed_figures_and_ignores_whitespace(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The dry run says which committed claims the current CV would stop matching."""
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    cv_text = master_cv.read_bytes().decode("utf-8")
    service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )
    job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        _evaluation_citing(str(workspace.id), cv_text, CITED_QUOTE),
    )

    changed = service.impact(str(workspace.id), cv_text.replace("90 seconds", "30 seconds"))

    # Posting evidence quotes the advertisement, so only the candidate citation counts.
    assert changed.citation_count == 1
    assert changed.material_count == 1
    assert changed.identical is False
    assert changed.omitted_count == 0
    citation = changed.citations[0]
    assert citation.employer == "Acme Health"
    assert citation.title == "Product Manager, Data Platform"
    assert citation.quote == CITED_QUOTE
    assert citation.drift.material is True
    assert "30 seconds" in citation.drift.text

    cosmetic = service.impact(
        str(workspace.id), cv_text.replace("## Summary\n\n", "## Summary\n\n\n")
    )

    assert cosmetic.citation_count == 1
    assert cosmetic.material_count == 0
    assert cosmetic.changed_count == 0
    assert cosmetic.unchanged_count == 1
    assert cosmetic.citations[0].drift.found is True

    deleted = service.impact(str(workspace.id), cv_text.replace(CITED_QUOTE, ""))

    assert deleted.citation_count == 1
    assert deleted.not_found_count == 1
    assert deleted.unchanged_count == 0
    assert deleted.citations[0].drift.found is False
    counted = (
        deleted.material_count
        + deleted.changed_count
        + deleted.unchanged_count
        + deleted.not_found_count
    )
    assert counted == deleted.citation_count

    # Nothing above wrote anything: the impact report is a dry run.
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(MasterCvVersion)) == 1


def test_impact_only_reports_drift_introduced_by_the_proposed_edit(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace = _workspace(durable_database, tmp_path)
    service = MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    cv_text = master_cv.read_bytes().decode("utf-8")
    service.import_source(
        workspace_id=str(workspace.id), filename="CV.md", content=master_cv.read_bytes()
    )
    job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        _evaluation_citing(str(workspace.id), cv_text, CITED_QUOTE),
    )
    current_text = cv_text.replace("## Summary", "## Executive Summary")
    service.save_edit(str(workspace.id), current_text)

    impact = service.impact(str(workspace.id), current_text)

    assert impact.identical is True
    assert impact.material_count == 0
    assert impact.changed_count == 0
    assert impact.not_found_count == 0
    assert impact.unchanged_count == 1

    already_changed = current_text.replace("90 seconds", "30 seconds")
    service.save_edit(str(workspace.id), already_changed)

    unchanged_impact = service.impact(str(workspace.id), already_changed)

    assert unchanged_impact.material_count == 0
    assert unchanged_impact.changed_count == 0
    assert unchanged_impact.not_found_count == 0
    assert unchanged_impact.unchanged_count == 1
