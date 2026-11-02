"""Replaceable artifact storage with strict local path boundaries."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from tailorcv.application.failures import ValidationError
from tailorcv.infrastructure.artifacts.files import private_directory, sync_directory


@dataclass(frozen=True)
class ArtifactDescriptor:
    """Content-addressed metadata persisted separately from file bytes."""

    storage_uri: str
    byte_size: int
    sha256: str
    mime_type: str


@dataclass
class StagedArtifact:
    """Private bytes awaiting a database outcome and atomic promotion."""

    store: LocalArtifactStore
    target: Path
    temporary: Path | None
    descriptor: ArtifactDescriptor

    @property
    def pending(self) -> bool:
        """Return whether bytes still need promotion."""
        return self.temporary is not None

    def finalize(self) -> ArtifactDescriptor:
        """Promote staged bytes idempotently without replacing divergent content."""
        if self.temporary is None:
            return self.descriptor
        if self.target.exists():
            existing = self.store.inspect(self.target)
            if (
                existing.sha256 != self.descriptor.sha256
                or existing.byte_size != self.descriptor.byte_size
            ):
                raise ValidationError(
                    f"Artifact destination already contains different bytes: {self.target}"
                )
            self.temporary.unlink(missing_ok=True)
            self.temporary = None
            return existing
        private_directory(self.target.parent)
        self.temporary.replace(self.target)
        self.target.chmod(0o600)
        sync_directory(self.target.parent)
        self.temporary = None
        return self.descriptor

    def discard(self) -> None:
        """Remove only private staged bytes after a database rollback."""
        if self.temporary is not None:
            self.temporary.unlink(missing_ok=True)
            self.temporary = None


class LocalArtifactStore:
    """Private local artifact store with explicit read-only import roots."""

    def __init__(self, root: Path, *, readable_roots: tuple[Path, ...] = ()) -> None:
        """Configure a writable root and optional external roots allowed for indexing."""
        self.root = root.expanduser().resolve()
        private_directory(self.root)
        self.readable_roots = tuple(item.expanduser().resolve() for item in readable_roots)

    def put(self, relative_path: str, content: bytes) -> ArtifactDescriptor:
        """Write one artifact atomically; absolute and traversing paths are refused."""
        staged = self.stage(relative_path, content)
        return staged.finalize()

    def stage(self, relative_path: str, content: bytes) -> StagedArtifact:
        """Write private staged bytes for later idempotent promotion."""
        target = self._write_target(relative_path)
        descriptor = _descriptor(target, content)
        if target.exists():
            existing = self.inspect(target)
            if existing.sha256 != descriptor.sha256 or existing.byte_size != descriptor.byte_size:
                raise ValidationError(
                    f"Artifact destination already contains different bytes: {target}"
                )
            return StagedArtifact(self, target, None, existing)
        staging = private_directory(self.root / ".staging")
        durable_stage = self.staging_path(relative_path, descriptor.sha256)
        if durable_stage.exists():
            existing = self.inspect(durable_stage)
            if existing.sha256 == descriptor.sha256 and existing.byte_size == descriptor.byte_size:
                return StagedArtifact(self, target, durable_stage, descriptor)
            raise ValidationError(f"Artifact staging collision at {durable_stage}.")
        handle, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=staging)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(durable_stage)
            sync_directory(staging)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return StagedArtifact(self, target, durable_stage, descriptor)

    def staging_path(self, relative_path: str, sha256: str) -> Path:
        """Return the deterministic private stage for one destination and digest."""
        self._write_target(relative_path)
        stage_name = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
        return self.root / ".staging" / f"{stage_name}-{sha256}.stage"

    def reconcile(self, path: Path, sha256: str, byte_size: int) -> ArtifactDescriptor:
        """Finalize recoverable staged bytes or verify an already promoted target."""
        target = path.expanduser().resolve(strict=False)
        if not _is_within(target, self.root):
            raise ValidationError(f"Artifact is outside configured storage roots: {target}")
        relative = target.relative_to(self.root).as_posix()
        expected = ArtifactDescriptor(
            storage_uri=target.as_uri(),
            byte_size=byte_size,
            sha256=sha256,
            mime_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        )
        # The stage is checked before the target, so a promotion that renames it
        # away between the two checks is still seen as a promoted target.
        staged_path = self.staging_path(relative, sha256)
        if staged_path.is_file():
            actual = self.inspect(staged_path)
            if actual.sha256 != sha256 or actual.byte_size != byte_size:
                raise ValidationError(f"Staged artifact for {target} failed its integrity check.")
            return StagedArtifact(self, target, staged_path, expected).finalize()
        if not target.exists():
            raise ValidationError(f"Artifact {target} has no recoverable staged bytes.")
        actual = self.inspect(target)
        if actual.sha256 != sha256 or actual.byte_size != byte_size:
            raise ValidationError(f"Artifact {target} differs from its durable descriptor.")
        return actual

    def inspect(self, path: Path) -> ArtifactDescriptor:
        """Hash one regular file inside the writable or explicitly readable roots."""
        _, descriptor = self.read(path)
        return descriptor

    def read(self, path: Path) -> tuple[bytes, ArtifactDescriptor]:
        """Read and hash one regular file inside configured storage boundaries."""
        resolved = path.expanduser().resolve(strict=True)
        allowed = (self.root, *self.readable_roots)
        if not any(_is_within(resolved, root) for root in allowed):
            raise ValidationError(f"Artifact is outside configured storage roots: {resolved}")
        if not resolved.is_file():
            raise ValidationError(f"Artifact is not a regular file: {resolved}")
        content = resolved.read_bytes()
        digest = hashlib.sha256(content)
        size = len(content)
        mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        descriptor = ArtifactDescriptor(
            storage_uri=resolved.as_uri(),
            byte_size=size,
            sha256=digest.hexdigest(),
            mime_type=mime_type,
        )
        return content, descriptor

    def _write_target(self, relative_path: str) -> Path:
        logical = PurePosixPath(relative_path)
        if logical.is_absolute() or not logical.parts or ".." in logical.parts:
            raise ValidationError("Artifact paths must be non-empty relative paths without '..'.")
        target = self.root.joinpath(*logical.parts).resolve()
        if not _is_within(target, self.root):
            raise ValidationError("Artifact path escapes the configured storage root.")
        return target


def _is_within(path: Path, root: Path) -> bool:
    """Return whether a resolved path is the root or one of its descendants."""
    return path == root or root in path.parents


def _descriptor(path: Path, content: bytes) -> ArtifactDescriptor:
    """Describe bytes at their eventual immutable path."""
    return ArtifactDescriptor(
        storage_uri=path.as_uri(),
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


def uri_scheme(reference: str) -> str:
    """Return the scheme a stored reference names, or "" for a plain path."""
    return urlsplit(reference).scheme


def file_uri_path(storage_uri: str) -> Path | None:
    """Resolve a local file URI without accepting remote authorities or schemes."""
    parsed = urlsplit(storage_uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    return Path(unquote(parsed.path)).resolve(strict=False)
