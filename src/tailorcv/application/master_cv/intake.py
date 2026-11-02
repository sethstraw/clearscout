"""Validate and prepare one master-CV source for durable registration."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tailorcv.application.config import Settings
from tailorcv.application.failures import (
    FREE_MODE_REASON,
    ConflictError,
    FreeModeError,
    NotFoundError,
    ValidationError,
)
from tailorcv.application.master_cv.cv import LIST_MARKER, CvInventory, parse_cv
from tailorcv.application.workspaces import WorkspaceSettingsService, layout_for, prepare_layout
from tailorcv.infrastructure.artifacts.files import (
    replace_private,
    write_private,
    write_private_bytes,
)
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Workspace
from tailorcv.infrastructure.providers.credentials import Prompter
from tailorcv.infrastructure.web_fetch.ingest import IMAGE_SUFFIXES
from tailorcv.shared.costs import Ledger
from tailorcv.shared.errors import CvParseError, TailorcvError

MAX_CV_BYTES = 25 * 1024 * 1024
SUPPORTED_SUFFIXES = frozenset({".docx", ".pdf", ".md", ".markdown", ".txt"}) | IMAGE_SUFFIXES
CANONICAL_FILENAME = "CV_Master.md"


def parse_canonical_source(source: Path, content: bytes) -> CvInventory | None:
    """Return an inventory only when a text source already meets the contract.

    A file that parses but holds no dated role, or whose list items the parser
    ran together into one entry, goes to the model instead of being adopted.
    """
    if source.suffix.lower() not in {".md", ".markdown", ".txt"}:
        return None
    try:
        inventory = parse_cv(source, text=content.decode("utf-8"))
    except (UnicodeDecodeError, CvParseError):
        return None
    return None if canonical_problem(inventory) else inventory


def canonical_problem(inventory: CvInventory) -> str | None:
    """Return why a parsed CV falls short of the canonical contract, or None."""
    if not any(role.dates for role in inventory.roles):
        return "it has no dated role"
    if any(LIST_MARKER.match(entry.text) for entry in inventory.entries):
        return "an entry starts with a list marker, so list items ran together"
    return None


def publish_canonical_master_cv(
    canonical: Path, markdown: str, existing_content: bytes | None
) -> None:
    """Publish a committed master CV as the workspace convenience copy."""
    if existing_content is None:
        write_private(canonical, markdown)
    elif existing_content != markdown.encode("utf-8"):
        replace_private(canonical, markdown)


@dataclass(frozen=True)
class PreparedMasterCvSource:
    """Validated source material ready for durable master-CV registration."""

    source_path: Path
    canonical_path: Path
    markdown: str
    inventory: CvInventory
    report_path: Path | None
    method: str
    existing_content: bytes | None
    ledger: Ledger = field(default_factory=Ledger)


class MasterCvIntake:
    """Own source validation, optional standardization, and private source retention."""

    def __init__(
        self,
        database: Database,
        storage: RuntimeStorage,
        settings: WorkspaceSettingsService,
        *,
        paid_work_ready: Callable[[], bool] | None = None,
    ) -> None:
        """Configure the workspace state needed to prepare imported source bytes.

        With ``paid_work_ready`` set, a source that needs the model is refused while it
        answers False.
        """
        self.database = database
        self.storage = storage
        self.settings = settings
        self.paid_work_ready = paid_work_ready

    def prepare(
        self,
        *,
        workspace_id: str,
        filename: str,
        content: bytes,
        settings: Settings | None,
        api_key: str | None,
        prompter: Prompter | None,
        replace: bool,
    ) -> PreparedMasterCvSource:
        """Validate, retain, and standardize source bytes only when required."""
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or safe_name != filename or suffix not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise ValidationError(f"Master CV filename must use one of: {supported}.")
        if not content or len(content) > MAX_CV_BYTES:
            raise ValidationError("A master CV must contain between 1 byte and 25 MiB.")

        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
            layout = layout_for(
                workspace.slug,
                root=workspace.root_path,
                storage=self.storage,
            )
            prepare_layout(layout, private_root=False)

        digest = hashlib.sha256(content).hexdigest()
        source_path = layout.imports_directory / f"{Path(safe_name).stem}-{digest[:12]}{suffix}"
        if source_path.exists() and source_path.read_bytes() != content:
            raise ValidationError("The retained CV source failed its content-hash check.")
        if not source_path.exists():
            write_private_bytes(source_path, content)
        inventory = parse_canonical_source(source_path, content)
        report: Path | None = None
        method = "adopted"
        spent = Ledger()
        if inventory is not None:
            markdown = content.decode("utf-8")
        else:
            if self.paid_work_ready is not None and not self.paid_work_ready():
                raise FreeModeError(FREE_MODE_REASON)
            method = "standardized"
            markdown, inventory, report, spent = self._standardize(
                workspace_id=workspace_id,
                work_directory=layout.work_directory,
                source_path=source_path,
                digest=digest,
                settings=settings,
                api_key=api_key,
                prompter=prompter,
            )

        existing_content = layout.master_cv.read_bytes() if layout.master_cv.exists() else None
        if (
            existing_content is not None
            and existing_content != markdown.encode("utf-8")
            and not replace
        ):
            raise ConflictError(
                "This workspace already has a different master CV. Confirm replacement and retry."
            )
        return PreparedMasterCvSource(
            source_path=source_path,
            canonical_path=layout.master_cv,
            markdown=markdown,
            inventory=inventory,
            report_path=report,
            method=method,
            existing_content=existing_content,
            ledger=spent,
        )

    def _standardize(
        self,
        *,
        workspace_id: str,
        work_directory: Path,
        source_path: Path,
        digest: str,
        settings: Settings | None,
        api_key: str | None,
        prompter: Prompter | None,
    ) -> tuple[str, CvInventory, Path | None, Ledger]:
        """Convert a source the parser cannot adopt, refusing a failed conversion."""
        staging = work_directory / f"cv-import-{digest[:12]}"
        destination = staging / "CV_Master.md"
        effective_settings = settings or self.settings.current(workspace_id)
        try:
            from tailorcv.application.tailoring.pipeline import import_cv

            imported = import_cv(
                source_path,
                effective_settings,
                destination=destination,
                api_key=api_key,
                prompter=prompter,
                cache_enabled=effective_settings.cache,
                force=True,
            )
        except TailorcvError as error:
            raise ValidationError(str(error)) from error
        if not imported.passed:
            raise ValidationError(
                "The CV was converted but did not pass authenticity and loss checks. "
                f"Review {imported.report or staging} before trying again."
            )
        markdown = imported.markdown
        return markdown, parse_cv(destination, text=markdown), imported.report, imported.ledger
