from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from clearscout.application.documents import DocumentService
from clearscout.application.documents.approvals import ApprovalService
from clearscout.application.documents.commands import DocumentVersionCommands
from clearscout.application.documents.promotion import DocumentArtifactPromotion
from clearscout.application.documents.queries import DocumentQueries
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.tasks import TaskQueueService, validate_task_payload
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.domain import ArtifactLogicalType
from clearscout.infrastructure.artifacts import rendering
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    ApprovalDecision,
    BackgroundTask,
    JobEvaluation,
    ReviewProposal,
    TailoringRun,
)
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import studio_workspace


def test_document_and_approval_services_have_single_responsibilities() -> None:
    assert not hasattr(DocumentService, "review")
    assert not hasattr(DocumentService, "decide")
    assert callable(ApprovalService.review)
    assert callable(ApprovalService.decide)


def test_the_review_carries_recorded_advisories_and_reads_an_older_run_as_having_none(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="advisory-run",
    )
    findings = [
        {"check": "factual review", "outcome": "warn", "detail": "Human review is required."},
        {
            "check": "critic",
            "outcome": "pass",
            "detail": "alignment 4/5",
            "advisories": ["The Acme migration has no outcome."],
        },
    ]
    with durable_database.session() as session:
        run = TailoringRun(
            workspace_id=workspace_id,
            job_posting_id=job_id,
            master_cv_version_id=master_version_id,
            task_id=task.id,
            outcome="passed",
            manifest={"run": {"findings": findings}},
        )
        session.add(run)
        session.flush()
        run_id = run.id

    review = ApprovalService(durable_database).review(run_id, workspace_id)

    assert [(item.check, item.advisories) for item in review.checks] == [
        ("factual review", []),
        ("critic", ["The Acme migration has no outcome."]),
    ]


def test_document_service_composes_commands_queries_and_promotion(
    durable_database: Database, tmp_path: Path
) -> None:
    service = DocumentService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )

    assert isinstance(service.commands, DocumentVersionCommands)
    assert isinstance(service.queries, DocumentQueries)
    assert isinstance(service.promotion, DocumentArtifactPromotion)


def test_document_versions_record_citation_override_and_freeze_legacy(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    approval_service = ApprovalService(durable_database)
    with pytest.raises(ValidationError, match="supplied together"):
        service.list_artifacts(workspace_id, entity_type="application")
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="document-version-run",
    )
    with durable_database.session() as session:
        stored_task = session.get(BackgroundTask, task.id)
        assert stored_task is not None
        stored_task.status = "completed"
        run = TailoringRun(
            workspace_id=workspace_id,
            job_posting_id=job_id,
            master_cv_version_id=master_version_id,
            task_id=task.id,
            outcome="passed",
            manifest={},
        )
        session.add(run)
        session.flush()
        proposal = ReviewProposal(
            tailoring_run_id=run.id,
            proposal_key="p01",
            slot="experience/one/bullet[0]",
            text="Built systems.",
            source_ids=["E1"],
            digest="a" * 32,
            payload={"id": "p01"},
        )
        session.add(proposal)
        session.flush()
        base = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=run.id,
            logical_type="resume.markdown",
            mime_type="text/markdown",
            content=b"# Candidate\nBuilt systems.\n",
            provenance={
                "prompt_suite": "prompt-a",
                "master_cv_hash": "cv-a",
                "line_citations": [
                    {
                        "line_number": 2,
                        "text": "Built systems.",
                        "source_ids": ["E1"],
                        "proposal_id": proposal.id,
                    }
                ],
            },
        )
        base_id = base.id
        run_id = run.id
        proposal_id = proposal.id

        uncatalogued = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=run.id,
            logical_type="cover_letter.markdown",
            mime_type="text/markdown",
            content=b"One\nTwo\n",
            provenance={
                "line_citations": [
                    "not-a-citation",
                    {"line_number": 0, "text": "invalid", "source_ids": ["E0"]},
                    {"line_number": 2, "text": "Two", "source_ids": []},
                    {"line_number": 2, "text": "Two", "source_ids": ["E2"]},
                    {"line_number": 1, "text": "One", "source_ids": ["E1"]},
                ]
            },
        )
        uncatalogued_id = uncatalogued.id
        malformed_citations = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=run.id,
            logical_type="tailoring.review.markdown",
            mime_type="text/markdown",
            content=b"Unchanged\n",
            provenance={"line_citations": "not-a-list"},
        )
        malformed_citations_id = malformed_citations.id

    check = service.citation_check(base_id, workspace_id, "# Candidate\nChanged systems.\n")
    assert check.requires_override is True
    with pytest.raises(ConflictError, match="override reason"):
        service.save_version(
            artifact_id=base_id,
            workspace_id=workspace_id,
            content="# Candidate\nChanged systems.\n",
            author_type="human",
        )
    saved = service.save_version(
        artifact_id=base_id,
        workspace_id=workspace_id,
        content="# Candidate\nChanged systems.\n",
        author_type="human",
        override_reason="I verified the revised wording myself.",
    )
    assert saved.version_number == 2
    assert saved.supersedes_artifact_id == base_id
    assert len(service.versions(str(saved.id), workspace_id)) == 2
    assert "-Built systems." in service.diff(base_id, str(saved.id), workspace_id).diff
    generated_override = service.save_version(
        artifact_id=uncatalogued_id,
        workspace_id=workspace_id,
        content="Changed\nTwo\n",
        author_type="human",
        override_reason="I confirmed this newly supplied wording.",
    )
    assert generated_override.version_number == 2
    assert (
        service.citation_check(malformed_citations_id, workspace_id, "Changed\n").requires_override
        is False
    )
    with durable_database.session() as session:
        decisions = session.scalars(select(ApprovalDecision)).all()
        assert len(decisions) == 2
        assert {item.decision for item in decisions} == {"replace"}

        detached = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="tailoring.report.markdown",
            mime_type="text/markdown",
            content=b"Evidence-backed\n",
            provenance={
                "line_citations": [
                    {"line_number": 1, "text": "Evidence-backed", "source_ids": ["E1"]}
                ]
            },
        )
        detached_id = detached.id
        invalid_utf8 = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="master_cv.snapshot.text",
            mime_type="text/plain",
            content=b"\xff",
            provenance={},
        )
        invalid_utf8_id = invalid_utf8.id
    assert service.versions(detached_id, workspace_id)[0].id == detached_id
    with pytest.raises(ConflictError, match="originating tailoring run"):
        service.save_version(
            artifact_id=detached_id,
            workspace_id=workspace_id,
            content="Changed\n",
            author_type="human",
            override_reason="Verified",
        )
    with pytest.raises(ValidationError, match="valid UTF-8"):
        service.diff(invalid_utf8_id, detached_id, workspace_id)
    with pytest.raises(NotFoundError, match="does not exist"):
        approval_service.review("missing", workspace_id)
    with pytest.raises(NotFoundError, match="does not exist"):
        approval_service.decide(
            "missing",
            workspace_id,
            proposal_digest="missing",
            decision="reject",
            replacement_text=None,
            replacement_source_ids=[],
        )
    review = approval_service.review(run_id, workspace_id)
    assert review.proposals[0].decisions[0].decision == "replace"
    accepted = approval_service.decide(
        proposal_id,
        workspace_id,
        proposal_digest="a" * 32,
        decision="accept",
        replacement_text=None,
        replacement_source_ids=[],
    )
    repeated = approval_service.decide(
        proposal_id,
        workspace_id,
        proposal_digest="a" * 32,
        decision="accept",
        replacement_text=None,
        replacement_source_ids=[],
    )
    assert repeated.id == accepted.id
    with pytest.raises(ValidationError, match="accept, reject, or replace"):
        approval_service.decide(
            proposal_id,
            workspace_id,
            proposal_digest="a" * 32,
            decision="skip",
            replacement_text=None,
            replacement_source_ids=[],
        )
    with pytest.raises(ValidationError, match="requires replacement_text"):
        approval_service.decide(
            proposal_id,
            workspace_id,
            proposal_digest="a" * 32,
            decision="replace",
            replacement_text="",
            replacement_source_ids=[],
        )
    with pytest.raises(ValidationError, match="Only a replacement"):
        approval_service.decide(
            proposal_id,
            workspace_id,
            proposal_digest="a" * 32,
            decision="accept",
            replacement_text="not allowed",
            replacement_source_ids=[],
        )
    with pytest.raises(ConflictError, match="changed"):
        approval_service.decide(
            proposal_id,
            workspace_id,
            proposal_digest="stale",
            decision="reject",
            replacement_text=None,
            replacement_source_ids=[],
        )
    queue = TaskQueueService(durable_database)
    render_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload={"artifact_id": invalid_utf8_id},
        idempotency_key="non-markdown-render-refused",
    )
    worker = Worker(
        durable_database,
        worker_id="non-markdown-render",
        handlers=default_handlers(durable_database, artifact_root=artifact_root),
    )
    assert worker.run_once()
    refused = queue.get(str(render_task.id))
    assert refused.status == "failed"
    assert "requires a Markdown artifact" in str(refused.error_message)


def test_document_upload_metadata_version_and_archive_paths(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, application_id, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    uploaded = service.upload(
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
        logical_type="recruiter.email",
        filename="message.txt",
        mime_type="text/plain",
        content=b"Please schedule a call.\n",
    )
    artifact_id = str(uploaded.id)
    assert not hasattr(service.get(artifact_id, workspace_id), "storage_uri")
    assert service.read(artifact_id, workspace_id).content.startswith(b"Please")
    assert (
        service.list_artifacts(workspace_id, entity_type="application", entity_id=application_id)[
            0
        ].id
        == artifact_id
    )
    assert service.versions(artifact_id, workspace_id)[0].id == artifact_id
    assert (
        service.relabel(artifact_id, workspace_id, "recruiter.email").logical_type
        == "recruiter.email"
    )

    with durable_database.session() as session:
        first = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="user.attachment",
            mime_type="text/plain",
            content=b"first\n",
            provenance={"source": "test"},
        )
        first_id = first.id
    with durable_database.session() as session:
        second = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="user.attachment",
            mime_type="text/plain",
            content=b"second\n",
            provenance={"source": "test"},
        )
        second_id = second.id
        assert second.version_number == 2
        pdf = service.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="document.rendered.pdf",
            mime_type="application/pdf",
            content=b"%PDF-test",
            provenance={"source": "test"},
        )
        pdf_id = pdf.id
    assert "-first" in service.diff(first_id, second_id, workspace_id).diff
    reverted = service.revert(
        current_artifact_id=second_id,
        target_artifact_id=first_id,
        workspace_id=workspace_id,
    )
    assert reverted.version_number == 3
    assert (
        service.citation_check(str(reverted.id), workspace_id, "first\n").requires_override is False
    )
    with pytest.raises(ValidationError, match="same document family"):
        service.revert(
            current_artifact_id=second_id,
            target_artifact_id=artifact_id,
            workspace_id=workspace_id,
        )
    with pytest.raises(ValidationError, match="only for Markdown"):
        service.diff(pdf_id, second_id, workspace_id)
    with pytest.raises(ValidationError, match="only for Markdown"):
        service.citation_check(pdf_id, workspace_id, "changed")
    with pytest.raises(ValidationError, match="attributed"):
        service.save_version(
            artifact_id=second_id,
            workspace_id=workspace_id,
            content="third\n",
            author_type="system",
        )
    with pytest.raises(ValidationError, match="cannot be empty"):
        service.upload(
            workspace_id=workspace_id,
            entity_type="application",
            entity_id=application_id,
            logical_type="user.attachment",
            filename="empty.txt",
            mime_type="text/plain",
            content=b"",
        )
    with pytest.raises(ValidationError, match="limited"):
        service.upload(
            workspace_id=workspace_id,
            entity_type="application",
            entity_id=application_id,
            logical_type="user.attachment",
            filename="large.bin",
            mime_type="application/octet-stream",
            content=b"x" * (25 * 1024 * 1024 + 1),
        )
    with pytest.raises(ValidationError, match="cannot be attached"):
        service.upload(
            workspace_id=workspace_id,
            entity_type="contact",
            entity_id="missing",
            logical_type="user.attachment",
            filename="invalid.txt",
            mime_type="text/plain",
            content=b"content",
        )
    fallback_name = service.upload(
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
        logical_type="user.attachment",
        filename="..",
        mime_type="",
        content=b"binary",
    )
    assert fallback_name.provenance["filename"] == "upload.bin"
    with pytest.raises(ValidationError, match="cannot be empty"):
        service.save_version(
            artifact_id=second_id,
            workspace_id=workspace_id,
            content="",
            author_type="human",
        )
    with pytest.raises(ValidationError, match="editable"):
        service.save_version(
            artifact_id=pdf_id,
            workspace_id=workspace_id,
            content="not a pdf",
            author_type="human",
        )
    service.archive(artifact_id, workspace_id)
    assert artifact_id not in {
        str(item.id)
        for item in service.list_artifacts(
            workspace_id, entity_type="application", entity_id=application_id
        )
    }
    assert artifact_id in {
        str(item.id)
        for item in service.list_artifacts(
            workspace_id,
            entity_type="application",
            entity_id=application_id,
            include_archived=True,
        )
    }


def test_an_upload_attaches_itself_to_the_evaluation_it_names(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    with durable_database.session() as session:
        evaluation = JobEvaluation(
            job_posting_id=job_id,
            master_cv_version_id=master_version_id,
            decision="investigate",
            rationale="One synthetic gap.",
            payload_sha256="b" * 64,
            evaluated_at=datetime.now(UTC),
        )
        session.add(evaluation)
        session.flush()
        evaluation_id = evaluation.id

    uploaded = service.upload(
        workspace_id=workspace_id,
        entity_type="evaluation",
        entity_id=evaluation_id,
        logical_type="user.attachment",
        filename="evaluation.txt",
        mime_type="text/plain",
        content=b"Evaluation attachment",
    )

    assert uploaded.related_entity_type == "evaluation"


def test_document_render_task_indexes_pressume_outputs(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, application_id, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    with durable_database.session() as session:
        source = documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="resume.markdown",
            mime_type="text/markdown",
            content=b"# Candidate\n",
            provenance={"source": "test"},
        )
        source_id = source.id
        pdf_source = documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="document.rendered.pdf",
            mime_type="application/pdf",
            content=b"%PDF-source",
            provenance={"source": "test"},
        )
        pdf_source_id = pdf_source.id
    application_markdown = documents.upload(
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
        logical_type="user.attachment",
        filename="note.md",
        mime_type="text/markdown",
        content=b"# Application note\n",
    )

    def fake_render(document: Path, *, letter: bool = False) -> rendering.Rendered:
        pdf = document.with_suffix(".pdf")
        text = document.with_suffix(".txt")
        pdf.write_bytes(b"%PDF-rendered")
        text.write_text("Candidate\n", encoding="utf-8")
        return rendering.Rendered(
            attempted=True,
            ok=True,
            message="Rendered document.pdf, document.txt",
            produced=(pdf, text),
            warnings=("pressume warned: readability: the final page is lightly filled",),
        )

    monkeypatch.setattr("clearscout.application.documents.render.rendering.render", fake_render)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload={"artifact_id": source_id},
        idempotency_key="render-success",
    )
    worker = Worker(
        durable_database,
        worker_id="render-success",
        handlers=default_handlers(durable_database, artifact_root=artifact_root),
    )
    assert worker.run_once()
    result = queue.get(str(task.id))
    assert result.status == "completed", result.error_message
    assert {item["logical_type"] for item in result.progress["artifacts"]} == {
        "document.rendered.pdf",
        "document.rendered.text",
    }
    assert "Rendered document.pdf" in result.progress["message"]
    assert "the final page is lightly filled" in result.progress["message"]
    assert all(
        item["provenance"]["derived_from_artifact_id"] == source_id
        for item in result.progress["artifacts"]
    )

    monkeypatch.setattr(
        "clearscout.application.documents.render.rendering.render",
        lambda _document, letter=False: rendering.Rendered(
            attempted=False, ok=False, message="pressume unavailable"
        ),
    )
    failed_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload={"artifact_id": source_id},
        idempotency_key="render-failure",
    )
    assert worker.run_once()
    failed = queue.get(str(failed_task.id))
    assert failed.status == "failed"
    assert failed.error_message == "pressume unavailable"

    for identifier, key, message in (
        (pdf_source_id, "render-pdf-refused", "requires a Markdown"),
        (
            str(application_markdown.id),
            "render-application-refused",
            "attached to a job posting",
        ),
    ):
        refused_task = queue.enqueue(
            workspace_id=workspace_id,
            task_type="document.render",
            payload={"artifact_id": identifier},
            idempotency_key=key,
        )
        assert worker.run_once()
        refused = queue.get(str(refused_task.id))
        assert refused.status == "failed"
        assert message in str(refused.error_message)


def test_a_render_task_names_the_job_its_document_is_filed_under(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, application_id, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    with durable_database.session() as session:
        source_id = documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="resume.markdown",
            mime_type="text/markdown",
            content=b"# Candidate\n",
            provenance={"source": "test"},
        ).id
    note = documents.upload(
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
        logical_type="user.attachment",
        filename="note.md",
        mime_type="text/markdown",
        content=b"# Application note\n",
    )
    queue = TaskQueueService(durable_database)

    queued = queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload=validate_task_payload("document.render", {"artifact_id": source_id}),
        idempotency_key="render-linked",
    )
    unlinked = queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload={"artifact_id": str(note.id)},
        idempotency_key="render-unlinked",
    )

    assert queued.job_posting_ids == [job_id]
    listed = {str(task.id): task.job_posting_ids for task in queue.list_tasks(workspace_id)}
    assert listed[str(queued.id)] == [job_id]
    assert unlinked.job_posting_ids == []
    with pytest.raises(ValidationError):
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="document.render",
            payload={"artifact_id": source_id, "job_posting_id": "job-elsewhere"},
            idempotency_key="render-misfiled",
        )


def test_document_render_names_outputs_as_a_package_and_renders_letters_as_letters(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    sources: dict[str, str] = {}
    with durable_database.session() as session:
        for logical_type in ("resume.markdown", "cover_letter.markdown"):
            sources[logical_type] = documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_id,
                tailoring_run_id=None,
                logical_type=logical_type,
                mime_type="text/markdown",
                content=b"# Candidate\n",
                provenance={"source": "test"},
            ).id
    asked: list[tuple[str, bool]] = []

    def fake_render(document: Path, *, letter: bool = False) -> rendering.Rendered:
        asked.append((document.name, letter))
        produced = (document.with_suffix(".pdf"), document.with_suffix(".txt"))
        for path in produced:
            path.write_bytes(b"rendered")
        return rendering.Rendered(attempted=True, ok=True, message="Rendered", produced=produced)

    monkeypatch.setattr("clearscout.application.documents.render.rendering.render", fake_render)
    queue = TaskQueueService(durable_database)
    worker = Worker(
        durable_database,
        worker_id="render-package",
        handlers=default_handlers(durable_database, artifact_root=artifact_root),
    )
    names: dict[str, list[str]] = {}
    for logical_type, source_id in sources.items():
        task = queue.enqueue(
            workspace_id=workspace_id,
            task_type="document.render",
            payload={"artifact_id": source_id},
            idempotency_key=f"render-{logical_type}",
        )
        assert worker.run_once()
        result = queue.get(str(task.id))
        assert result.status == "completed", result.error_message
        names[logical_type] = sorted(
            item["provenance"]["relative_path"] for item in result.progress["artifacts"]
        )

    assert asked == [("Resume.md", False), ("Cover_Letter.md", True)]
    assert names == {
        "resume.markdown": ["Resume.pdf", "Resume.txt"],
        "cover_letter.markdown": ["Cover_Letter.pdf", "Cover_Letter.txt"],
    }


def test_a_rendered_file_joins_the_family_of_the_document_it_renders(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))

    def draft(logical_type: ArtifactLogicalType, content: bytes) -> str:
        with durable_database.session() as session:
            return documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_id,
                tailoring_run_id=None,
                logical_type=logical_type,
                mime_type="text/markdown",
                content=content,
                provenance={"source": "test"},
            ).id

    def fake_render(document: Path, *, letter: bool = False) -> rendering.Rendered:
        pdf = document.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-" + document.read_bytes())
        return rendering.Rendered(attempted=True, ok=True, message="Rendered", produced=(pdf,))

    monkeypatch.setattr("clearscout.application.documents.render.rendering.render", fake_render)
    queue = TaskQueueService(durable_database)
    worker = Worker(
        durable_database,
        worker_id="render-families",
        handlers=default_handlers(durable_database, artifact_root=artifact_root),
    )

    def render(source_id: str, key: str) -> str:
        task = queue.enqueue(
            workspace_id=workspace_id,
            task_type="document.render",
            payload={"artifact_id": source_id},
            idempotency_key=key,
        )
        assert worker.run_once()
        result = queue.get(str(task.id))
        assert result.status == "completed", result.error_message
        return str(result.progress["artifacts"][0]["id"])

    resume_pdf = render(draft("resume.markdown", b"# Resume one\n"), "render-resume")
    letter_pdf = render(draft("cover_letter.markdown", b"# Letter\n"), "render-letter")
    rerendered_pdf = render(draft("resume.markdown", b"# Resume two\n"), "render-resume-again")
    redrafted_letter_pdf = render(
        draft("cover_letter.markdown", b"# Letter two\n"), "render-letter-again"
    )
    edited = documents.save_version(
        artifact_id=documents.get(rerendered_pdf, workspace_id).provenance[
            "derived_from_artifact_id"
        ],
        workspace_id=workspace_id,
        content="# Resume three\n",
        author_type="human",
    )
    edited_pdf = render(str(edited.id), "render-resume-edited")

    def history(artifact_id: str) -> list[tuple[str, int | None, str | None]]:
        return [
            (item.id, item.version_number, item.supersedes_artifact_id)
            for item in documents.versions(artifact_id, workspace_id)
        ]

    assert history(redrafted_letter_pdf) == [
        (redrafted_letter_pdf, 2, letter_pdf),
        (letter_pdf, 1, None),
    ]
    assert history(edited_pdf) == [
        (edited_pdf, 3, rerendered_pdf),
        (rerendered_pdf, 2, resume_pdf),
        (resume_pdf, 1, None),
    ]


def test_a_render_numbers_past_every_version_its_family_already_holds(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    drafts: tuple[tuple[ArtifactLogicalType, bytes], ...] = (
        ("resume.markdown", b"# Resume\n"),
        ("cover_letter.markdown", b"# Letter\n"),
    )
    with durable_database.session() as session:
        resume_id, letter_id = (
            documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_id,
                tailoring_run_id=None,
                logical_type=logical_type,
                mime_type="text/markdown",
                content=content,
                provenance={"source": "test"},
            ).id
            for logical_type, content in drafts
        )
        # One family holding a resume render at v1 and a letter render at v2.
        mixed = [
            documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_id,
                tailoring_run_id=None,
                logical_type="document.rendered.pdf",
                mime_type="application/pdf",
                content=b"%PDF-" + source.encode(),
                provenance={"source": "pressume"},
            )
            for source in (resume_id, letter_id)
        ]
        for artifact, source in zip(mixed, (resume_id, letter_id), strict=True):
            artifact.provenance = {"source": "pressume", "derived_from_artifact_id": source}
        family = str(mixed[0].artifact_family_id)
        resume_render_id = mixed[0].id

    def fake_render(document: Path, *, letter: bool = False) -> rendering.Rendered:
        pdf = document.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-rerendered")
        return rendering.Rendered(attempted=True, ok=True, message="Rendered", produced=(pdf,))

    monkeypatch.setattr("clearscout.application.documents.render.rendering.render", fake_render)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload={"artifact_id": resume_id},
        idempotency_key="render-into-mixed-family",
    )
    worker = Worker(
        durable_database,
        worker_id="render-mixed-family",
        handlers=default_handlers(durable_database, artifact_root=artifact_root),
    )
    assert worker.run_once()
    result = queue.get(str(task.id))

    assert result.status == "completed", result.error_message
    rendered = result.progress["artifacts"][0]
    assert (rendered["artifact_family_id"], rendered["version_number"]) == (family, 3)
    assert rendered["supersedes_artifact_id"] == resume_render_id
