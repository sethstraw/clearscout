from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import func, select

from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.failures import ValidationError
from clearscout.application.workspaces import WorkspaceService
from clearscout.infrastructure.artifacts.store import LocalArtifactStore, StagedArtifact
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Artifact
from tests.support.artifacts import runtime_storage, strand_promotion


def test_artifact_store_writes_private_content_atomically(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    content = b"durable evidence\n"

    descriptor = store.put("workspace/report.txt", content)
    written = tmp_path / "artifacts" / "workspace" / "report.txt"

    assert written.read_bytes() == content
    assert descriptor.storage_uri == written.resolve().as_uri()
    assert descriptor.sha256 == hashlib.sha256(content).hexdigest()
    assert descriptor.byte_size == len(content)
    assert descriptor.mime_type == "text/plain"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert not list(written.parent.glob(".report.txt.*"))


@pytest.mark.parametrize("logical", ["", "../escape.txt", "/absolute.txt", "a/../../b"])
def test_artifact_store_refuses_unsafe_write_paths(tmp_path: Path, logical: str) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValidationError, match="Artifact path"):
        store.put(logical, b"no")


def test_artifact_store_indexes_only_configured_read_roots(tmp_path: Path) -> None:
    imported = tmp_path / "legacy" / "resume.pdf"
    imported.parent.mkdir()
    imported.write_bytes(b"pdf")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    store = LocalArtifactStore(tmp_path / "artifacts", readable_roots=(imported.parent,))

    assert store.inspect(imported).byte_size == 3
    with pytest.raises(ValidationError, match="outside configured"):
        store.inspect(outside)


def test_artifact_destination_is_idempotent_and_never_replaced(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    first = store.put("workspace/report.txt", b"first")

    assert store.put("workspace/report.txt", b"first") == first
    with pytest.raises(ValidationError, match="different bytes"):
        store.put("workspace/report.txt", b"second")
    assert (tmp_path / "artifacts" / "workspace" / "report.txt").read_bytes() == b"first"


def test_promotion_refuses_a_destination_that_changed_under_it(tmp_path: Path) -> None:
    """An artifact path is immutable, including between staging and promotion.

    The idempotent case is checked at `stage`, but the window that matters is
    the one after it: bytes are on disk, the database has committed, and only
    then does the target turn out to hold something else.
    """
    store = LocalArtifactStore(tmp_path / "artifacts")
    staged = store.stage("workspace/report.txt", b"first")
    target = tmp_path / "artifacts" / "workspace" / "report.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"second")

    with pytest.raises(ValidationError, match="different bytes"):
        staged.finalize()

    assert target.read_bytes() == b"second"
    # The staged bytes are kept, so the refusal is reportable and recoverable
    # rather than a silent loss.
    assert staged.pending


def test_promotion_is_satisfied_by_a_destination_that_already_matches(tmp_path: Path) -> None:
    """The same bytes arriving twice is a completed write, not a conflict."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    staged = store.stage("workspace/report.txt", b"first")
    target = tmp_path / "artifacts" / "workspace" / "report.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"first")

    descriptor = staged.finalize()

    assert descriptor.sha256 == hashlib.sha256(b"first").hexdigest()
    assert descriptor.storage_uri == target.resolve().as_uri()
    assert not staged.pending
    assert not list((tmp_path / "artifacts" / ".staging").glob("*.stage"))
    # Promotion is idempotent, so finalizing again is not a second write.
    assert staged.finalize() == descriptor


def test_staging_is_reused_for_identical_bytes_and_refuses_a_corrupted_stage(
    tmp_path: Path,
) -> None:
    """One stage per destination and digest, and it must still be those bytes."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    staging = tmp_path / "artifacts" / ".staging"
    first = store.stage("workspace/report.txt", b"durable evidence")
    second = store.stage("workspace/report.txt", b"durable evidence")

    assert first.temporary is not None
    assert second.temporary == first.temporary
    assert len(list(staging.glob("*.stage"))) == 1

    # A stage file that survived a crash mid-write has the right name and the
    # wrong bytes, so promoting it would put unverified content at a fixed path.
    first.temporary.write_bytes(b"truncated")
    with pytest.raises(ValidationError, match="staging collision"):
        store.stage("workspace/report.txt", b"durable evidence")


def test_a_failed_stage_write_leaves_no_partial_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that cannot be made durable is not a write."""

    def explode(descriptor: int) -> None:
        raise OSError("synthetic disk failure")

    store = LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr("clearscout.infrastructure.artifacts.store.os.fsync", explode)

    with pytest.raises(OSError, match="synthetic disk failure"):
        store.stage("workspace/report.txt", b"durable evidence")

    assert list((tmp_path / "artifacts" / ".staging").iterdir()) == []
    assert not (tmp_path / "artifacts" / "workspace").exists()


def test_reconcile_verifies_a_promoted_artifact_against_its_descriptor(tmp_path: Path) -> None:
    """Reconciliation is an integrity check, not a repair."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    descriptor = store.put("workspace/report.txt", b"durable evidence")
    target = tmp_path / "artifacts" / "workspace" / "report.txt"

    assert store.reconcile(target, descriptor.sha256, descriptor.byte_size) == descriptor

    target.write_bytes(b"tampered")
    with pytest.raises(ValidationError, match="differs from its durable descriptor"):
        store.reconcile(target, descriptor.sha256, descriptor.byte_size)


def test_reconcile_promotes_staged_bytes_left_by_an_interrupted_write(tmp_path: Path) -> None:
    """The compensation path: the index survived, the promotion did not."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    staged = store.stage("workspace/report.txt", b"durable evidence")
    target = tmp_path / "artifacts" / "workspace" / "report.txt"
    assert not target.exists()

    descriptor = store.reconcile(target, staged.descriptor.sha256, staged.descriptor.byte_size)

    assert descriptor == staged.descriptor
    assert target.read_bytes() == b"durable evidence"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list((tmp_path / "artifacts" / ".staging").glob("*.stage"))


def test_reconcile_refuses_staged_bytes_that_fail_their_integrity_check(tmp_path: Path) -> None:
    """Recoverable is not the same as intact."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    staged = store.stage("workspace/report.txt", b"durable evidence")
    assert staged.temporary is not None
    staged.temporary.write_bytes(b"truncated")
    target = tmp_path / "artifacts" / "workspace" / "report.txt"

    with pytest.raises(ValidationError, match="failed its integrity check"):
        store.reconcile(target, staged.descriptor.sha256, staged.descriptor.byte_size)

    assert not target.exists()


def test_reconcile_sees_a_promotion_that_lands_while_it_runs_as_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    staged = store.stage("workspace/report.txt", b"durable evidence")
    target = tmp_path / "artifacts" / "workspace" / "report.txt"
    locate = LocalArtifactStore.staging_path

    def promote_concurrently(self: LocalArtifactStore, relative_path: str, sha256: str) -> Path:
        staged.finalize()
        return locate(self, relative_path, sha256)

    monkeypatch.setattr(LocalArtifactStore, "staging_path", promote_concurrently)

    descriptor = store.reconcile(target, staged.descriptor.sha256, staged.descriptor.byte_size)

    assert descriptor.sha256 == staged.descriptor.sha256
    assert target.read_bytes() == b"durable evidence"


def test_reconcile_refuses_what_it_cannot_account_for(tmp_path: Path) -> None:
    """Nothing to verify and nothing to recover is an unresolved artifact."""
    imported = tmp_path / "legacy"
    imported.mkdir()
    (imported / "resume.pdf").write_bytes(b"pdf")
    store = LocalArtifactStore(tmp_path / "artifacts", readable_roots=(imported,))
    digest = hashlib.sha256(b"pdf").hexdigest()

    with pytest.raises(ValidationError, match="no recoverable staged bytes"):
        store.reconcile(tmp_path / "artifacts" / "workspace" / "report.txt", digest, 3)

    # A readable import root can be indexed and hashed, but it is not somewhere
    # this store may promote bytes into.
    with pytest.raises(ValidationError, match="outside configured storage roots"):
        store.reconcile(imported / "resume.pdf", digest, 3)


def test_database_rollback_discards_private_staging_without_index_or_target(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="rollback", name="Rollback"
    )
    service = EvidenceArtifactService(runtime_storage(durable_database, tmp_path / "artifacts"))
    target = tmp_path / "artifacts" / "rollback" / "evidence.txt"

    def store_then_fault() -> None:
        with durable_database.session() as session:
            service.store(
                session,
                workspace_id=str(workspace.id),
                logical_type="posting.body.text",
                content=b"synthetic evidence",
                relative_path="rollback/evidence.txt",
                mime_type="text/plain",
                related_entity_type="test",
                related_entity_id="synthetic",
                provenance={"source": "test"},
                audit={"created_by": "test", "origin": "fault_injection"},
            )
            raise RuntimeError("fault after file stage")

    with pytest.raises(RuntimeError, match="fault after file stage"):
        store_then_fault()

    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(Artifact)) == 0
    assert not target.exists()
    assert not list((tmp_path / "artifacts" / ".staging").glob("*.stage"))


def test_a_store_committed_by_an_exclusive_session_is_promoted_and_verified(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="exclusive", name="Exclusive"
    )
    service = EvidenceArtifactService(runtime_storage(durable_database, tmp_path / "artifacts"))

    with durable_database.exclusive_session() as session:
        artifact_id = service.store(
            session,
            workspace_id=str(workspace.id),
            logical_type="posting.body.text",
            content=b"synthetic evidence",
            relative_path="exclusive/evidence.txt",
            mime_type="text/plain",
            related_entity_type="test",
            related_entity_id="synthetic",
            provenance={"source": "test"},
            audit={"created_by": "test", "origin": "exclusive_store"},
        ).id

    with durable_database.session() as session:
        stored = session.get(Artifact, artifact_id)
        assert stored is not None and stored.integrity_status == "verified"
    assert (tmp_path / "artifacts" / "exclusive" / "evidence.txt").read_bytes() == (
        b"synthetic evidence"
    )


def test_failed_post_commit_promotion_stays_indexed_and_reconcilable(
    durable_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="recover", name="Recover"
    )
    service = EvidenceArtifactService(runtime_storage(durable_database, tmp_path / "artifacts"))
    original = StagedArtifact.finalize

    def fail_once(self: StagedArtifact):
        raise OSError("synthetic promotion failure")

    stored: list[str] = []

    def store_and_commit() -> None:
        with durable_database.session() as session:
            artifact = service.store(
                session,
                workspace_id=str(workspace.id),
                logical_type="posting.body.text",
                content=b"synthetic evidence",
                relative_path="recover/evidence.txt",
                mime_type="text/plain",
                related_entity_type="test",
                related_entity_id="synthetic",
                provenance={"source": "test"},
                audit={"created_by": "test", "origin": "fault_injection"},
            )
            stored.append(artifact.id)

    monkeypatch.setattr(StagedArtifact, "finalize", fail_once)
    with pytest.raises(OSError, match="synthetic promotion failure"):
        store_and_commit()

    [artifact_id] = stored

    with durable_database.session() as session:
        indexed = session.get(Artifact, artifact_id)
        assert indexed is not None and indexed.integrity_status == "staged"
    assert not (tmp_path / "artifacts" / "recover" / "evidence.txt").exists()
    assert list((tmp_path / "artifacts" / ".staging").glob("*.stage"))

    monkeypatch.setattr(StagedArtifact, "finalize", original)
    assert service.reconcile(durable_database) == {
        "finalized": 1,
        "unresolved": 0,
        "outside_root": 0,
        "retrying": 0,
    }
    with durable_database.session() as session:
        reconciled = session.get(Artifact, artifact_id)
        assert reconciled is not None and reconciled.integrity_status == "verified"


_STRANDED = b"synthetic evidence"
_STRANDED_PATH = "stranded/evidence/body.txt"


def _drop_stage(target: Path, stage: Path) -> None:
    stage.unlink()


def _corrupt_stage(target: Path, stage: Path) -> None:
    stage.write_bytes(b"truncated")


def _occupy_target(target: Path, stage: Path) -> None:
    target.write_bytes(b"different")


def _promote_without_indexing(target: Path, stage: Path) -> None:
    stage.replace(target)


def _block_promotion(target: Path, stage: Path) -> None:
    # A file where the target's directory belongs fails promotion even for root.
    target.parent.rmdir()
    target.parent.write_bytes(b"")


def _stranded_row(database: Database, root: Path) -> tuple[str, Path]:
    """Index one staged artifact under root and strand it before promotion."""
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="stranded", name="Stranded"
    )
    service = EvidenceArtifactService(runtime_storage(database, root))
    with database.session() as session:
        artifact_id = service.store(
            session,
            workspace_id=str(workspace.id),
            logical_type="posting.body.text",
            content=_STRANDED,
            relative_path=_STRANDED_PATH,
            mime_type="text/plain",
            related_entity_type="test",
            related_entity_id="synthetic",
            provenance={"source": "test"},
            audit={"created_by": "test", "origin": "fault_injection"},
        ).id
    return artifact_id, strand_promotion(database, root, artifact_id)


@pytest.mark.parametrize(
    ("strand", "status", "counted", "target_bytes"),
    [
        (_drop_stage, "unresolved", "unresolved", None),
        (_corrupt_stage, "unresolved", "unresolved", None),
        (_occupy_target, "unresolved", "unresolved", b"different"),
        (_promote_without_indexing, "verified", "finalized", _STRANDED),
        (_block_promotion, "staged", "retrying", None),
    ],
    ids=["stage-missing", "stage-mismatched", "target-differs", "target-promoted", "io-failure"],
)
def test_reconcile_settles_a_stranded_row_by_what_it_finds(
    durable_database: Database,
    tmp_path: Path,
    strand: Callable[[Path, Path], None],
    status: str,
    counted: str,
    target_bytes: bytes | None,
) -> None:
    root = tmp_path / "artifacts"
    artifact_id, target = _stranded_row(durable_database, root)
    stage = LocalArtifactStore(root).staging_path(
        _STRANDED_PATH, hashlib.sha256(_STRANDED).hexdigest()
    )
    strand(target, stage)

    outcome = EvidenceArtifactService(runtime_storage(durable_database, root)).reconcile(
        durable_database
    )

    with durable_database.session() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None and artifact.integrity_status == status
    assert (target.read_bytes() if target.exists() else None) == target_bytes
    assert outcome == {
        key: int(key == counted) for key in ("finalized", "unresolved", "outside_root", "retrying")
    }


def test_reconcile_leaves_a_row_outside_its_root_staged(
    durable_database: Database, tmp_path: Path
) -> None:
    """A start against another artifact root never settles rows that root does not hold."""
    artifact_id, _target = _stranded_row(durable_database, tmp_path / "artifacts")

    outcome = EvidenceArtifactService(
        runtime_storage(durable_database, tmp_path / "scratch")
    ).reconcile(durable_database)

    with durable_database.session() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None and artifact.integrity_status == "staged"
    assert outcome == {"finalized": 0, "unresolved": 0, "outside_root": 1, "retrying": 0}
