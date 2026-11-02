"""Read, edit, version, and resolve the prompts a run is produced under.

The packaged file is the default and this table holds only overrides, so a
prompt nobody has edited has no row. Saving appends, and reverting appends a
copy rather than moving a pointer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tailorcv.application.documents import unified
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.prompts import (
    FIELDS,
    PROMPT_NAMES,
    canonical,
    check_template,
    digest,
    packaged,
)
from tailorcv.application.workspaces.prompt_models import (
    PromptDetailResponse,
    PromptDiffResponse,
    PromptStateResponse,
    PromptVersionResponse,
)
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PromptTemplate, PromptVersion, Workspace
from tailorcv.shared.errors import ConfigError

PACKAGED = "packaged"
OVERRIDE = "override"

# What a later change to the packaged default means for one override.
# ``adopted`` is the case where the stored copy never diverged, so the new
# default is taken; ``collision`` is where both moved.
CURRENT = "current"
ADOPTED = "adopted"
COLLISION = "collision"

# Version zero names the packaged file wherever a version number is accepted.
PACKAGED_VERSION = 0


def _template_for(session: Session, workspace_id: str, key: str) -> PromptTemplate | None:
    """Return the override record for one prompt, archived or not."""
    return session.scalar(
        select(PromptTemplate).where(
            PromptTemplate.workspace_id == workspace_id,
            PromptTemplate.key == key,
        )
    )


def _latest_version(session: Session, template: PromptTemplate | None) -> PromptVersion | None:
    """Return the newest saved version of one override."""
    if template is None:
        return None
    return session.scalar(
        select(PromptVersion)
        .where(PromptVersion.prompt_template_id == template.id)
        .order_by(PromptVersion.version_number.desc())
        .limit(1)
    )


def _still_diverges(current: PromptVersion | None, base: str) -> bool:
    """Report whether a stored copy differs from the packaged text it was forked from."""
    if current is None:
        return False
    return not (current.content_digest == current.base_digest and current.base_digest != base)


def _effective_version(
    session: Session, key: str, template: PromptTemplate | None
) -> PromptVersion | None:
    """Return the version that actually runs, or ``None`` for the packaged file.

    An archived override is a reset, so the packaged file answers again.
    """
    if template is None or template.archived_at is not None:
        return None
    current = _latest_version(session, template)
    return current if _still_diverges(current, _packaged_digest(key)) else None


def overrides_in(session: Session, workspace_id: str) -> dict[str, tuple[str, str]]:
    """Return the prompt text in force, reading inside a caller's transaction.

    Keyed by prompt name, valued by the version identifier and its text, so one
    read answers both what a run is given and what it records. Takes a session
    because a workflow already in a transaction must not open a second
    connection to the same SQLite file.
    """
    latest = (
        select(
            PromptVersion.prompt_template_id,
            func.max(PromptVersion.version_number).label("version_number"),
        )
        .group_by(PromptVersion.prompt_template_id)
        .subquery()
    )
    rows = session.execute(
        select(PromptTemplate.key, PromptVersion)
        .join(latest, latest.c.prompt_template_id == PromptTemplate.id)
        .join(
            PromptVersion,
            (PromptVersion.prompt_template_id == PromptTemplate.id)
            & (PromptVersion.version_number == latest.c.version_number),
        )
        .where(
            PromptTemplate.workspace_id == workspace_id,
            PromptTemplate.archived_at.is_(None),
            PromptTemplate.key.in_(PROMPT_NAMES),
        )
    ).all()
    return {
        key: (version.id, version.content)
        for key, version in rows
        if _still_diverges(version, _packaged_digest(key))
    }


def _packaged_digest(key: str) -> str:
    """Return the content identifier of the prompt text that ships with the package."""
    return digest(canonical(packaged(key)))


class PromptLibraryService:
    """One workspace's prompt overrides and their append-only history."""

    def __init__(self, database: Database) -> None:
        """Bind the library to one durable database."""
        self.database = database

    def list_prompts(self, workspace_id: str) -> list[PromptStateResponse]:
        """Return every prompt in the suite and where its current text came from."""
        with self.database.session() as session:
            _workspace(session, workspace_id)
            return [
                PromptStateResponse.model_validate(_state(session, workspace_id, key))
                for key in PROMPT_NAMES
            ]

    def read(self, workspace_id: str, key: str) -> PromptDetailResponse:
        """Return one prompt's resolved text beside the packaged text it may replace."""
        with self.database.session() as session:
            _workspace(session, workspace_id)
            _known(key)
            return PromptDetailResponse.model_validate(_detail(session, workspace_id, key))

    def versions(self, workspace_id: str, key: str) -> list[PromptVersionResponse]:
        """Return every saved version of one prompt, newest first.

        Each row says which earlier version it restored, when its content
        matches one. That is read out of the digests rather than recorded at
        save time, so it stays true for a revert nobody labeled as one.
        """
        with self.database.session() as session:
            _workspace(session, workspace_id)
            _known(key)
            template = _template_for(session, workspace_id, key)
            if template is None:
                return []
            rows = list(
                session.scalars(
                    select(PromptVersion)
                    .where(PromptVersion.prompt_template_id == template.id)
                    .order_by(PromptVersion.version_number.desc())
                ).all()
            )
            earliest: dict[str, int] = {}
            restored: dict[str, int | None] = {}
            for row in reversed(rows):
                restored[row.id] = earliest.get(row.content_digest)
                earliest.setdefault(row.content_digest, row.version_number)
            return [
                PromptVersionResponse(
                    id=row.id,
                    key=key,
                    version=row.version_number,
                    content_sha256=row.content_digest,
                    base_sha256=row.base_digest,
                    created_at=row.created_at.isoformat(),
                    created_by=row.created_by,
                    origin=row.origin,
                    restores_version=restored[row.id],
                )
                for row in rows
            ]

    def diff(
        self, workspace_id: str, key: str, *, from_version: int, to_version: int
    ) -> PromptDiffResponse:
        """Return a unified diff between any two versions of one prompt.

        Any two, not two adjacent ones, and version zero is the packaged file,
        so "what have I changed" and "what did I undo in March" are the same
        request with different numbers.
        """
        with self.database.session() as session:
            _workspace(session, workspace_id)
            _known(key)
            template = _template_for(session, workspace_id, key)
            return PromptDiffResponse(
                key=key,
                from_version=from_version,
                to_version=to_version,
                diff=unified(
                    _text_at(session, key, template, from_version),
                    _text_at(session, key, template, to_version),
                    _label(from_version),
                    _label(to_version),
                ),
            )

    def save(
        self,
        workspace_id: str,
        key: str,
        content: str,
        *,
        expected_version: int | None = None,
        actor: str = "local_user",
        origin: str = "tailorcv",
        correlation_id: str | None = None,
    ) -> PromptDetailResponse:
        """Append an override, refusing text the calling code could not fill.

        A stale ``expected_version`` is refused, so two tabs editing one prompt
        cannot silently overwrite each other.
        """
        with self.database.session() as session:
            _workspace(session, workspace_id)
            _known(key)
            try:
                text = canonical(content)
                check_template(key, text)
            except ConfigError as error:
                raise ValidationError(str(error)) from error

            template = _template_for(session, workspace_id, key)
            current = _latest_version(session, template)
            live = _effective_version(session, key, template)
            in_force = PACKAGED_VERSION if live is None else live.version_number
            if expected_version is not None and expected_version != in_force:
                raise ConflictError(
                    f"Prompt {key} is at version {in_force}, not {expected_version}. "
                    "Reload before saving so the other edit is not lost."
                )

            base = _packaged_digest(key)
            content_digest = digest(text)
            # Saving unchanged text creates no override, so a later release has nothing
            # to collide with.
            unchanged = content_digest == (base if live is None else live.content_digest)
            if not unchanged:
                _append(
                    session,
                    workspace_id=workspace_id,
                    key=key,
                    template=template,
                    previous=current,
                    content=text,
                    content_digest=content_digest,
                    base_digest=base,
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                )
                session.flush()
            return PromptDetailResponse.model_validate(_detail(session, workspace_id, key))

    def revert(
        self,
        workspace_id: str,
        key: str,
        version: int,
        *,
        actor: str = "local_user",
        origin: str = "tailorcv",
        correlation_id: str | None = None,
    ) -> PromptDetailResponse:
        """Append a copy of an earlier version rather than moving a pointer.

        Reverting to version four appends version eighteen carrying version
        four's text. Nothing is rewritten and nothing is orphaned, and that a
        revert happened is recoverable from the content digests.
        """
        with self.database.session() as session:
            _workspace(session, workspace_id)
            _known(key)
            template = _template_for(session, workspace_id, key)
            text = _text_at(session, key, template, version)
            _append(
                session,
                workspace_id=workspace_id,
                key=key,
                template=template,
                previous=_latest_version(session, template),
                content=text,
                content_digest=digest(text),
                base_digest=_packaged_digest(key),
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.flush()
            return PromptDetailResponse.model_validate(_detail(session, workspace_id, key))

    def reset(self, workspace_id: str, key: str) -> PromptDetailResponse:
        """Archive the override so the packaged file answers again.

        A recorded action rather than a delete: the versions stay, the
        packaged text is what runs, and editing again continues the same
        history instead of starting a second one.
        """
        with self.database.session() as session:
            _workspace(session, workspace_id)
            _known(key)
            template = _template_for(session, workspace_id, key)
            if template is not None and template.archived_at is None:
                template.archived_at = utc_now()
                session.flush()
            return PromptDetailResponse.model_validate(_detail(session, workspace_id, key))

    def overrides(self, workspace_id: str) -> dict[str, tuple[str, str]]:
        """Return the text in force for every overridden prompt, and its version id."""
        with self.database.session() as session:
            _workspace(session, workspace_id)
            return overrides_in(session, workspace_id)


def _state(session: Session, workspace_id: str, key: str) -> dict[str, Any]:
    """Return where one prompt's text came from, without carrying its content."""
    _known(key)
    template = _template_for(session, workspace_id, key)
    current = _latest_version(session, template)
    base = _packaged_digest(key)
    live = template is not None and template.archived_at is None
    effective = current if live and _still_diverges(current, base) else None
    upgrade = CURRENT
    if live and current is not None and current.base_digest != base:
        upgrade = ADOPTED if effective is None else COLLISION
    return {
        "key": key,
        "workspace_id": workspace_id,
        "origin": PACKAGED if effective is None else OVERRIDE,
        "version": PACKAGED_VERSION if effective is None else effective.version_number,
        "content_sha256": digest(
            canonical(packaged(key) if effective is None else effective.content)
        ),
        "packaged_sha256": base,
        "base_sha256": "" if effective is None else effective.base_digest,
        "upgrade": upgrade,
        # The declaration, not the stored text: where a release has dropped a
        # placeholder, the declaration is what an editor has to enforce.
        "variables": [
            {"name": name, "handling": slot.handling.value} for name, slot in FIELDS[key].items()
        ],
        "updated_at": None if effective is None else effective.created_at.isoformat(),
        "updated_by": "" if effective is None else effective.created_by,
    }


def _detail(session: Session, workspace_id: str, key: str) -> dict[str, Any]:
    """Return one prompt's state together with both texts an editor needs."""
    effective = _effective_version(session, key, _template_for(session, workspace_id, key))
    return {
        **_state(session, workspace_id, key),
        "content": packaged(key) if effective is None else effective.content,
        "packaged_content": packaged(key),
    }


def _text_at(session: Session, key: str, template: PromptTemplate | None, version: int) -> str:
    """Return the text of one numbered version, where zero is the packaged file."""
    if version == PACKAGED_VERSION:
        return packaged(key)
    row = None
    if template is not None:
        row = session.scalar(
            select(PromptVersion).where(
                PromptVersion.prompt_template_id == template.id,
                PromptVersion.version_number == version,
            )
        )
    if row is None:
        raise NotFoundError(f"Prompt {key} has no version {version}.")
    return row.content


def _append(
    session: Session,
    *,
    workspace_id: str,
    key: str,
    template: PromptTemplate | None,
    previous: PromptVersion | None,
    content: str,
    content_digest: str,
    base_digest: str,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> None:
    """Write one immutable version, creating or reviving its template row."""
    if template is None:
        template = PromptTemplate(
            workspace_id=workspace_id,
            key=key,
            created_by=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        session.add(template)
        session.flush()
    elif template.archived_at is not None:
        template.archived_at = None
    session.add(
        PromptVersion(
            prompt_template_id=template.id,
            version_number=(previous.version_number if previous else 0) + 1,
            content=content,
            content_digest=content_digest,
            base_digest=base_digest,
            created_by=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
    )


def _known(key: str) -> None:
    """Refuse a prompt the program never calls, rather than storing one."""
    if key not in FIELDS:
        raise NotFoundError(f"Prompt {key!r} is not part of this prompt suite.")


def _workspace(session: Session, workspace_id: str) -> Workspace:
    """Return the workspace, or refuse rather than writing an orphan row."""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.archived_at is not None:
        raise NotFoundError(f"Workspace does not exist: {workspace_id}")
    return workspace


def _label(version: int) -> str:
    """Name one side of a diff the way a reader of it would."""
    return PACKAGED if version == PACKAGED_VERSION else f"v{version}"
