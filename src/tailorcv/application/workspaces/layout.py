"""User-owned filesystem layout for one TailorCV workspace."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from tailorcv.application.failures import ValidationError
from tailorcv.infrastructure.artifacts.files import private_directory
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage


@dataclass(frozen=True)
class WorkspaceLayout:
    """Stable paths created for one initialized workspace."""

    root: Path
    master_cv_directory: Path
    imports_directory: Path
    work_directory: Path
    backups_directory: Path

    @property
    def master_cv(self) -> Path:
        """Return the canonical master-CV path referenced by workspace settings."""
        return self.master_cv_directory / "CV_Master.md"

    @property
    def config(self) -> Path:
        """Return the workspace-local configuration path."""
        return self.root / "tailorcv.toml"


@dataclass
class StagedWorkspaceLayout:
    """Private workspace directory awaiting database commit."""

    layout: WorkspaceLayout
    staged_root: Path | None

    def finalize(self) -> WorkspaceLayout:
        """Promote a new workspace idempotently after its row commits."""
        if self.staged_root is None:
            return self.layout
        if self.layout.root.exists():
            raise ValidationError(
                f"Workspace destination appeared during creation: {self.layout.root}"
            )
        self.staged_root.replace(self.layout.root)
        self.staged_root = None
        return self.layout

    def discard(self) -> None:
        """Remove only the private stage created for a rolled-back workspace."""
        if self.staged_root is not None:
            shutil.rmtree(self.staged_root, ignore_errors=True)
            self.staged_root = None


def layout_for(
    slug: str,
    *,
    storage: RuntimeStorage,
    root: str | Path | None = None,
) -> WorkspaceLayout:
    """Resolve a workspace layout without changing the filesystem."""
    workspace_root = storage.workspace_root(slug, root)
    return WorkspaceLayout(
        root=workspace_root,
        master_cv_directory=workspace_root / "master-cv",
        imports_directory=workspace_root / "imports",
        work_directory=workspace_root / "work",
        backups_directory=workspace_root / "backups",
    )


def prepare_layout(layout: WorkspaceLayout, *, private_root: bool) -> WorkspaceLayout:
    """Create private application-owned subdirectories for a resolved layout."""
    if layout.root in {Path(layout.root.anchor), Path.home().resolve()}:
        raise ValidationError(
            "A workspace root must be a dedicated folder, not a home or disk root."
        )
    try:
        if private_root:
            private_directory(layout.root)
        else:
            layout.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            layout.master_cv_directory,
            layout.imports_directory,
            layout.work_directory,
            layout.backups_directory,
        ):
            private_directory(directory)
    except OSError as error:
        raise ValidationError(f"Cannot prepare workspace folder {layout.root}: {error}") from error
    return layout


def stage_layout(layout: WorkspaceLayout, token: str) -> StagedWorkspaceLayout:
    """Prepare a private sibling and leave the canonical root untouched."""
    if layout.root.exists():
        prepare_layout(layout, private_root=False)
        return StagedWorkspaceLayout(layout, None)
    private_directory(layout.root.parent)
    staged_root = layout.root.parent / f".{layout.root.name}.tailorcv-stage-{token}"
    staged = WorkspaceLayout(
        root=staged_root,
        master_cv_directory=staged_root / "master-cv",
        imports_directory=staged_root / "imports",
        work_directory=staged_root / "work",
        backups_directory=staged_root / "backups",
    )
    prepare_layout(staged, private_root=True)
    return StagedWorkspaceLayout(layout, staged_root)
