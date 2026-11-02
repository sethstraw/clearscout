"""One immutable resolution of TailorCV's local storage boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy.engine import make_url

from tailorcv.infrastructure.persistence.database import (
    DATABASE_ENVIRONMENT_VARIABLE,
    default_data_directory,
    sqlite_path,
)
from tailorcv.shared.errors import ConfigError


class WorkspaceRootPolicy(StrEnum):
    """How implicit workspace roots are selected."""

    DATA_ROOT = "data_root"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class RuntimeStorage:
    """Resolved database and filesystem locations used by one process.

    Resolution is deliberately eager. A process must not discover a database in
    one place and then let an artifact service independently fall back to the
    default Documents tree.
    """

    database_url: str
    data_root: Path
    artifact_root: Path
    workspaces_root: Path
    workspace_policy: WorkspaceRootPolicy
    read_only_import_roots: tuple[Path, ...] = ()

    @classmethod
    def resolve(
        cls,
        database_url: str | None = None,
        *,
        data_root: Path | None = None,
        artifact_root: Path | None = None,
        workspaces_root: Path | None = None,
        read_only_import_roots: tuple[Path, ...] = (),
    ) -> RuntimeStorage:
        """Resolve one coherent local boundary without creating anything."""
        explicit_data_root = data_root.expanduser().resolve() if data_root is not None else None
        configured = (database_url or "").strip()
        environment_url = os.environ.get(DATABASE_ENVIRONMENT_VARIABLE, "").strip()
        if not configured:
            configured = environment_url
        if not configured:
            root = explicit_data_root or default_data_directory().expanduser().resolve()
            configured = f"sqlite+pysqlite:///{root / 'tailorcv.sqlite3'}"

        path = sqlite_path(configured)
        if explicit_data_root is not None:
            root = explicit_data_root
        elif path is not None:
            root = path.parent
        elif artifact_root is not None:
            root = artifact_root.expanduser().resolve().parent
        else:
            backend = make_url(configured).drivername
            raise ConfigError(
                f"Database backend {backend!r} has no local filesystem boundary.\n"
                "  Pass --data-root or --artifact-root explicitly. Only SQLite is supported "
                "for public local operation."
            )

        artifacts = (
            artifact_root.expanduser().resolve()
            if artifact_root is not None
            else (root / "artifacts").resolve()
        )
        workspace_base = (
            workspaces_root.expanduser().resolve()
            if workspaces_root is not None
            else (root / "workspaces").resolve()
        )
        imports = tuple(
            dict.fromkeys(item.expanduser().resolve() for item in read_only_import_roots)
        )
        return cls(
            database_url=configured,
            data_root=root,
            artifact_root=artifacts,
            workspaces_root=workspace_base,
            workspace_policy=(
                WorkspaceRootPolicy.EXPLICIT
                if workspaces_root is not None
                else WorkspaceRootPolicy.DATA_ROOT
            ),
            read_only_import_roots=imports,
        )

    def workspace_root(self, slug: str, explicit: str | Path | None = None) -> Path:
        """Return one workspace root under the configured policy."""
        if explicit is not None:
            return Path(explicit).expanduser().resolve()
        return (self.workspaces_root / slug).resolve()

    def readable_roots(self, workspace_root: str | Path | None = None) -> tuple[Path, ...]:
        """Return owned plus explicitly allowed read-only roots for one workspace."""
        roots = [self.artifact_root, *self.read_only_import_roots]
        if workspace_root is not None:
            roots.append(Path(workspace_root).expanduser().resolve())
        return tuple(dict.fromkeys(roots))
