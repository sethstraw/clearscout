"""Keep the list of boards a workspace sweeps: add, correct, retire, and describe.

The list is filled two ways. A verified board found by search or evaluation is
remembered against its employer, and the user maintains the list directly. An
address naming no known platform is kept rather than refused, so an employer
worth tracking stays visible while each sweep reports that it needs a person.
"""

from __future__ import annotations

from typing import Any, get_args

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from clearscout.application.discovery.board_slugs import board_identity_key
from clearscout.application.discovery.pending import active_task_target_ids
from clearscout.application.failures import (
    CareerPageRefusedError,
    CareerPageUnreadableError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from clearscout.application.job_search.models import (
    READ_SCAN_STATUSES,
    SourceListResponse,
    SourcePreviewResponse,
    SourceResponse,
    SourceTerms,
)
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import SourceKind, SourceReadOutcome, utc_now
from clearscout.domain.identity import normalize_text, requisition_key
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    Employer,
    JobPosting,
    PendingDiscovery,
    PostingIdentifier,
    Source,
    SourceScan,
    Workspace,
)
from clearscout.infrastructure.web_fetch.board_adapters import (
    BOARD_ADAPTER_REGISTRY,
    BoardApiFetcher,
    BoardResolver,
    identify_board_url,
)
from clearscout.infrastructure.web_fetch.board_mappings.ukg import UKG_BOARD
from clearscout.infrastructure.web_fetch.board_platform import BoardKind
from clearscout.infrastructure.web_fetch.boards import (
    board_identity,
    canonicalise_source_url,
    job_board_address,
)
from clearscout.infrastructure.web_fetch.company_research import research_can_read
from clearscout.infrastructure.web_fetch.policy import (
    DestinationUnresolvableError,
    FetchPolicyError,
    HostHeldError,
    InvalidFetchUrlError,
    RobotsDeniedError,
    RobotsUnavailableError,
)

_KNOWN_PLATFORMS = frozenset(get_args(BoardKind))

# SQLite does not enforce the column's declared width, so the write does.
_NAME_WIDTH = Source.name.type.length or 300

NAME_CORRECTED_ACTION = "source.name_corrected"

# A source carries one of these origins only once a person stated or affirmed its
# employer name; every other origin holds a name some model or script proposed.
PERSON_ANSWERED_SOURCE_ORIGINS = frozenset({"api", "evaluation"})


def employer_name_is_authoritative(origin: str) -> bool:
    """Return whether a person answers for the employer name a source of ``origin`` holds."""
    return origin in PERSON_ANSWERED_SOURCE_ORIGINS


def source_read_address(url: str | None, resolved_url: str | None) -> tuple[str, str]:
    """Return the address a source is read at, and a note when its resolution was set aside.

    The resolution is read unless the source address itself names a board and the
    resolution names a different one; then the source address wins.
    """
    address, resolved = url or "", resolved_url or ""
    named = board_identity(address) if address else None
    if resolved and named is not None and board_identity(resolved) != named:
        return address, (
            f"The saved resolution {resolved} names a different board than the source "
            "address, so the source address was read."
        )
    return resolved or address, ""


def _known_platform(value: str | None) -> BoardKind | None:
    """Return ``value`` when it names a platform this build still recognizes."""
    return value if value in _KNOWN_PLATFORMS else None  # type: ignore[return-value]


def _url_platform(url: str) -> BoardKind | None:
    """Return the platform a board address names by its pattern alone, without a fetch."""
    found = identify_board_url(url)
    return None if found is None else found[0]


def _standing_refusal(exc: FetchPolicyError | httpx.HTTPError) -> bool:
    """Whether the site refuses this read outright, so retrying the same address cannot help."""
    if isinstance(exc, RobotsDeniedError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}


def _unreadable_cause(exc: FetchPolicyError | httpx.HTTPError) -> str:
    """Say in one clause why a career-page read was refused or failed."""
    if isinstance(exc, RobotsDeniedError):
        return "its robots.txt refuses this client"
    if isinstance(exc, RobotsUnavailableError):
        return "its robots.txt could not be read, so no read is allowed"
    if isinstance(exc, DestinationUnresolvableError):
        return "its host name does not resolve to a public address"
    if isinstance(exc, HostHeldError):
        return "the host asked for a wait between requests; try again later"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"the site answered HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "the site did not answer in time"
    if isinstance(exc, httpx.HTTPError):
        return "the connection to the site failed"
    return "published fetch policy refuses this read"


def _resolution_note(platform: BoardKind | None, url: str) -> str:
    """Describe recognition separately from a scan's observed coverage."""
    if platform is not None:
        return (
            f"Recognized {BOARD_ADAPTER_REGISTRY[platform].display_name} board. "
            "Scan results show actual coverage and any publisher limits."
        )
    if UKG_BOARD.search(url):
        return (
            "UKG individual posting pages are supported, "
            "but a full board listing is not available. "
            "Add individual posting links to read these openings."
        )
    return (
        "No named board system was recognized. Published posting pages may still be readable, "
        "but complete board coverage is not established. "
        "Add individual posting links or paste posting text when a scan cannot read the board."
    )


def _reads_board(source: Source, identity: str) -> bool:
    """Say whether a stored source's address or resolved address has this board identity."""
    addresses = (source.url, source.resolved_url)
    return identity in {board_identity_key(value) for value in addresses if value}


def _repeats_board(source: Source, source_type: SourceKind, identity: str) -> bool:
    """Say whether a stored source of this type already reads the board with this identity."""
    return source.source_type == source_type.value and _reads_board(source, identity)


class SourceCatalog:
    """Own every write to the sweep list and the reads that describe one source."""

    def __init__(self, database: Database, *, resolver: BoardResolver | None = None) -> None:
        """Bind the sweep list to one durable database and one board-resolution seam."""
        self.database = database
        self.resolver = resolver or BoardApiFetcher()

    def list_sources(self, workspace_id: str) -> SourceListResponse:
        """List the board sources this workspace sweeps, with each one's last outcome."""
        with self.database.session() as session:
            if session.get(Workspace, workspace_id) is None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
            rows = session.execute(
                select(Source, Employer)
                .outerjoin(Employer, Employer.id == Source.employer_id)
                .where(Source.workspace_id == workspace_id, Source.archived_at.is_(None))
                .order_by(Source.priority, Source.name)
            ).all()
            return SourceListResponse(
                items=[self._describe(session, source, employer) for source, employer in rows]
            )

    def add_source(
        self,
        workspace_id: str,
        name: str,
        url: str,
        *,
        source_type: SourceKind = SourceKind.EMPLOYER_BOARD,
        terms: SourceTerms | None = None,
        enabled: bool = True,
        origin: str,
        correlation_id: str | None = None,
    ) -> SourceResponse:
        """Register one source by hand, kept even when its address names no known platform.

        A career page that is refused or fails to load raises before anything is written.
        Raises ConflictError when the name is held at another address, archived or not,
        or by another source type. Re-adding the same board records only the terms sent.
        """
        source_name = name.strip()
        address = canonicalise_source_url(url)
        if not source_name:
            raise ValidationError("A source needs a name.")
        if not address:
            raise ValidationError("A source needs a board or careers address.")
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
            held = self._held_source(session, workspace_id, source_name, source_type, address)
            if held is not None:
                return self._settle_terms(session, *held, terms)
        platform, resolved_url = self._resolve(address, source_type)
        # The page read holds no lock, so the name is checked again under the write lock.
        with self.database.exclusive_session() as session:
            held = self._held_source(session, workspace_id, source_name, source_type, address)
            if held is not None:
                return self._settle_terms(session, *held, terms)
            remember = (
                self.remember_board_in_session
                if source_type is SourceKind.EMPLOYER_BOARD
                else self._remember_aggregator_in_session
            )
            source_id = self._restore_named_sibling(
                session, workspace_id, source_name, address, enabled=enabled
            ) or remember(
                session,
                workspace_id,
                source_name,
                address,
                enabled=enabled,
                origin=origin,
                correlation_id=correlation_id,
            )
            source, employer = self._locate(session, workspace_id, source_id)
            source.resolved_platform = platform
            source.resolved_url = resolved_url
            return self._settle_terms(session, source, employer, terms)

    def _restore_named_sibling(
        self,
        session: Session,
        workspace_id: str,
        name: str,
        address: str,
        *,
        enabled: bool,
    ) -> str | None:
        """Restore the archived board named by a hand add when another employer owns it.

        A guided sibling carries a suffixed name its employer's name does not derive,
        so remembering by that name would create a second employer and a duplicate name.
        """
        source = session.scalar(
            select(Source).where(
                Source.workspace_id == workspace_id,
                Source.name == name,
                Source.source_type == SourceKind.EMPLOYER_BOARD.value,
                Source.archived_at.is_not(None),
            )
        )
        if source is None or source.employer_id is None:
            return None
        if not _reads_board(source, board_identity_key(address)):
            return None
        derived = self._existing_employer(session, workspace_id, normalize_text(name[:300]))
        if derived is not None and derived.id == source.employer_id:
            return None
        owner = session.get(Employer, source.employer_id)
        if owner is not None:
            owner.archived_at = None
        source.url = address
        source.enabled = enabled
        source.archived_at = None
        return source.id

    def _settle_terms(
        self,
        session: Session,
        source: Source,
        employer: Employer | None,
        terms: SourceTerms | None,
    ) -> SourceResponse:
        """Record any terms sent with an add, then render the source."""
        if terms is not None:
            self._record_terms(source, terms)
        session.flush()
        return self._describe(session, source, employer)

    def preview_url(
        self, workspace_id: str, url: str, source_type: SourceKind = SourceKind.EMPLOYER_BOARD
    ) -> SourcePreviewResponse:
        """Resolve one address without saving anything, before a source exists."""
        with self.database.session() as session:
            if session.get(Workspace, workspace_id) is None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
        platform, resolved_url = self._resolve(url.strip(), source_type)
        return SourcePreviewResponse(
            resolved_url=resolved_url,
            platform=platform,
            note=_resolution_note(platform, resolved_url),
        )

    def resolve_platform(self, workspace_id: str, source_id: str) -> SourceResponse:
        """Re-run board resolution against one already-saved source address."""
        with self.database.session() as session:
            source, employer = self._locate(session, workspace_id, source_id)
            self._resolve_into(source)
            session.flush()
            return self._describe(session, source, employer)

    def _resolve_into(self, source: Source) -> None:
        """Resolve one source's saved address and record what was found."""
        platform, resolved_url = self._resolve(source.url, SourceKind(source.source_type))
        source.resolved_platform = platform
        source.resolved_url = resolved_url

    def _resolve(self, url: str, source_type: SourceKind) -> tuple[BoardKind | None, str]:
        """Identify the board platform an address names, fetching its page if worth it.

        An aggregator lists many employers and is bound to none of them, so its
        address is only checked against known patterns; fetching its page to
        look for one more board would not make it an employer board. Only an
        employer board address is worth a live page read.
        """
        if source_type is SourceKind.AGGREGATOR:
            return _url_platform(url), url
        try:
            resolution = self.resolver.resolve(url)
        except InvalidFetchUrlError as exc:
            raise ValidationError(
                f"{url} is not a public web address ClearScout may read."
            ) from exc
        except (FetchPolicyError, httpx.HTTPError) as exc:
            if _standing_refusal(exc):
                raise CareerPageRefusedError(url, _unreadable_cause(exc)) from exc
            raise CareerPageUnreadableError(url, _unreadable_cause(exc)) from exc
        return _known_platform(resolution.platform), resolution.url

    @staticmethod
    def _record_terms(source: Source, terms: SourceTerms) -> None:
        source.terms_url = terms.url.strip()
        source.terms_condition = terms.condition.strip()

    @staticmethod
    def _remember_aggregator_in_session(
        session: Session,
        workspace_id: str,
        name: str,
        url: str,
        *,
        enabled: bool = True,
        origin: str,
        correlation_id: str | None = None,
    ) -> str:
        """Persist one source that lists many employers and is bound to none."""
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or workspace.archived_at is not None:
            raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
        source = session.scalar(
            select(Source).where(
                Source.workspace_id == workspace_id,
                Source.name == name,
            )
        )
        if source is None:
            source = Source(
                workspace_id=workspace_id,
                employer_id=None,
                name=name,
                source_type=SourceKind.AGGREGATOR.value,
                url=url,
                priority=1,
                enabled=enabled,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.add(source)
            session.flush()
        else:
            source.url = url
            source.enabled = enabled
            source.archived_at = None
        return source.id

    def update_source(
        self,
        workspace_id: str,
        source_id: str,
        *,
        name: str | None = None,
        url: str | None = None,
        careers_url: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        terms: SourceTerms | None = None,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> SourceResponse:
        """Change one source's name, address, company careers page, sweep state, or order.

        A changed address is resolved again before anything is written, so a page
        that is refused or fails to load refuses the whole edit. Raises ConflictError
        when another edit changed the address while this one was resolving.
        """
        compared, resolved = (
            (None, None) if url is None else self._resolve_edit(workspace_id, source_id, url)
        )
        # A rename reads the in-flight guard before writing, so no task may start between.
        with self.database.exclusive_session() as session:
            source, employer = self._locate(session, workspace_id, source_id)
            if compared is not None and source.url != compared:
                raise ConflictError(
                    f"{source.name}'s address changed to {source.url} during this edit; "
                    "reload it and edit again."
                )
            if name is not None:
                self._rename(
                    session,
                    source,
                    employer,
                    name,
                    audit=audit_columns(actor, origin, correlation_id),
                )
            if resolved is not None:
                source.url, source.resolved_platform, source.resolved_url = resolved
            if careers_url is not None:
                self._set_careers_page(employer, careers_url)
            if enabled is not None:
                source.enabled = enabled
            if priority is not None:
                source.priority = priority
            if terms is not None:
                self._record_terms(source, terms)
            session.flush()
            return self._describe(session, source, employer)

    def _resolve_edit(
        self, workspace_id: str, source_id: str, url: str
    ) -> tuple[str, tuple[str, BoardKind | None, str] | None]:
        """Return the stored address compared against, and a changed address with its resolution.

        Resolution may fetch the career page and holds no database lock.
        """
        address = canonicalise_source_url(url)
        if not address:
            raise ValidationError("A source needs a board or careers address.")
        with self.database.session() as session:
            source, _ = self._locate(session, workspace_id, source_id)
            current, source_type = source.url, SourceKind(source.source_type)
        if address == current:
            return current, None
        platform, resolved_url = self._resolve(address, source_type)
        return current, (address, platform, resolved_url)

    @staticmethod
    def _set_careers_page(employer: Employer | None, careers_url: str) -> None:
        """Write or clear a person's company careers page, refusing one research would not read."""
        address = careers_url.strip()
        if employer is None:
            raise ValidationError(
                "This source lists many employers, so it has no company careers page."
            )
        if not address:
            employer.careers_url = None
            return
        if not research_can_read(address):
            raise ValidationError("Enter the full address, starting with https://")
        if job_board_address(address):
            raise ValidationError(
                "That address is on a job board or applicant tracking system. The company "
                "careers page is the employer's own site, which research reads to describe "
                "the company."
            )
        employer.careers_url = address

    def _rename(
        self,
        session: Session,
        source: Source,
        employer: Employer | None,
        name: str,
        *,
        audit: dict[str, Any],
    ) -> None:
        """Rename one source, its employer, that employer's staged rows and requisition keys.

        A blank or unchanged name writes nothing.

        Raises ConflictError when another row already holds the name, archived or
        not, because both uniqueness constraints count archived rows, and while
        paid discovery work holds one of the employer's staged rows.
        """
        wanted = name.strip()[:_NAME_WIDTH]
        if not wanted or wanted == source.name:
            return
        normalized = normalize_text(wanted)[:_NAME_WIDTH]
        self._refuse_held_name(session, source, employer, wanted, normalized)
        staged: list[str] = []
        if employer is not None:
            staged = self._staged_under(session, source.workspace_id, employer.name)
            if active_task_target_ids(session, source.workspace_id, staged):
                raise ConflictError(
                    "A guided search or discovery evaluation is in flight for a row staged "
                    f"under {employer.name}; wait for it to finish before renaming it."
                )
            self._restate_requisitions(session, employer, wanted)
        old = source.name
        source.name = wanted
        if employer is not None:
            if staged:
                session.execute(
                    update(PendingDiscovery)
                    .where(PendingDiscovery.id.in_(staged))
                    .values(employer=wanted)
                )
            employer.name = wanted
            employer.normalized_name = normalized
        session.add(
            AuditEvent(
                workspace_id=source.workspace_id,
                action=NAME_CORRECTED_ACTION,
                entity_type="source",
                entity_id=source.id,
                details={"old": old, "new": wanted, "restated_pending": len(staged)},
                **audit,
            )
        )

    @staticmethod
    def _staged_under(session: Session, workspace_id: str, name: str) -> list[str]:
        """Return the ids of every staged row carrying the employer name.

        Rows match by name, not by ``source_id``, which a search-staged row lacks
        and which names only the last source that listed a row.
        """
        wanted = normalize_text(name)
        return [
            row_id
            for row_id, employer in session.execute(
                select(PendingDiscovery.id, PendingDiscovery.employer).where(
                    PendingDiscovery.workspace_id == workspace_id
                )
            )
            if normalize_text(employer) == wanted
        ]

    @staticmethod
    def _restate_requisitions(session: Session, employer: Employer, new: str) -> None:
        """Rewrite the employer's stored requisition keys, which embed its name.

        Raises ConflictError, before writing any key, when another posting already
        holds one of the rewritten keys.
        """
        old_prefix = requisition_key(employer.name, "")
        new_prefix = requisition_key(new, "")
        if old_prefix == new_prefix:
            return
        restated = {
            identifier: new_prefix + identifier.identifier_value[len(old_prefix) :]
            for identifier in session.scalars(
                select(PostingIdentifier)
                .join(JobPosting, JobPosting.id == PostingIdentifier.job_posting_id)
                .where(
                    JobPosting.employer_id == employer.id,
                    PostingIdentifier.identifier_type == "requisition",
                )
            )
            if identifier.identifier_value.startswith(old_prefix)
        }
        if not restated:
            return
        holder = session.execute(
            select(PostingIdentifier.identifier_value, JobPosting.title, Employer.name)
            .join(JobPosting, JobPosting.id == PostingIdentifier.job_posting_id)
            .join(Employer, Employer.id == JobPosting.employer_id)
            .where(
                PostingIdentifier.workspace_id == employer.workspace_id,
                PostingIdentifier.identifier_type == "requisition",
                PostingIdentifier.identifier_value.in_(list(restated.values())),
            )
        ).first()
        if holder is not None:
            raise ConflictError(
                f"{holder.title} at {holder.name} already holds requisition "
                f"{holder.identifier_value[len(new_prefix) :]} under the name {new}."
            )
        for identifier, value in restated.items():
            identifier.identifier_value = value

    @staticmethod
    def _refuse_held_name(
        session: Session,
        source: Source,
        employer: Employer | None,
        wanted: str,
        normalized: str,
    ) -> None:
        """Refuse the rename when another source or employer already holds the name."""
        held = session.execute(
            select(Source.archived_at).where(
                Source.workspace_id == source.workspace_id,
                Source.name == wanted,
                Source.id != source.id,
            )
        ).first()
        if held is not None:
            if held[0] is not None:
                raise ConflictError(f"An archived source is already named {wanted}.")
            raise ConflictError(f"Another source is already named {wanted}.")
        if employer is None:
            return
        holder = session.execute(
            select(Employer.name, Employer.archived_at).where(
                Employer.workspace_id == source.workspace_id,
                Employer.normalized_name == normalized,
                Employer.id != employer.id,
            )
        ).first()
        if holder is None:
            return
        standing = "An archived employer" if holder.archived_at is not None else "Another employer"
        raise ConflictError(f"{standing}, {holder.name}, already holds the name {wanted}.")

    def archive_source(self, workspace_id: str, source_id: str) -> None:
        """Stop sweeping one board, keeping the scans it has already recorded."""
        with self.database.session() as session:
            source, _ = self._locate(session, workspace_id, source_id)
            source.archived_at = utc_now()

    def _locate(
        self, session: Session, workspace_id: str, source_id: str
    ) -> tuple[Source, Employer | None]:
        row = session.execute(
            select(Source, Employer)
            .outerjoin(Employer, Employer.id == Source.employer_id)
            .where(
                Source.id == source_id,
                Source.workspace_id == workspace_id,
                Source.archived_at.is_(None),
            )
        ).first()
        if row is None:
            raise NotFoundError(f"Unknown source {source_id!r}.")
        return row[0], row[1]

    def _held_source(
        self,
        session: Session,
        workspace_id: str,
        name: str,
        source_type: SourceKind,
        address: str,
    ) -> tuple[Source, Employer | None] | None:
        """Return the active source a hand add repeats, or None when the add may write.

        Raises ConflictError when the name is held at another board or by another type.
        """
        match = Source.name == name
        if source_type is SourceKind.EMPLOYER_BOARD:
            employer = self._existing_employer(session, workspace_id, normalize_text(name[:300]))
            if employer is not None:
                match = or_(match, Source.employer_id == employer.id)
        held = sorted(
            session.scalars(select(Source).where(Source.workspace_id == workspace_id, match)),
            key=lambda source: source.archived_at is not None,
        )
        if not held:
            return None
        identity = board_identity_key(address)
        repeated = [source for source in held if _repeats_board(source, source_type, identity)]
        holder = held[0]
        for source in repeated:
            if source.archived_at is None:
                owner = source.employer_id
                return source, None if owner is None else session.get(Employer, owner)
        # An archived board of exactly this name restores beside its active siblings.
        if repeated and (holder.archived_at is not None or any(s.name == name for s in repeated)):
            return None
        standing = "" if holder.archived_at is None else ", archived,"
        raise ConflictError(f"A source named {holder.name}{standing} already reads {holder.url}.")

    def _describe(
        self, session: Session, source: Source, employer: Employer | None
    ) -> SourceResponse:
        """Render one source and its latest scan outcome inside the caller's transaction."""
        latest = session.scalars(
            select(SourceScan)
            .where(SourceScan.source_id == source.id)
            .order_by(SourceScan.scanned_on.desc(), SourceScan.id)
            .limit(1)
        ).first()
        completed_read = SourceScan.status.in_(READ_SCAN_STATUSES)
        row = session.execute(
            select(
                func.count(SourceScan.id),
                func.coalesce(func.sum(SourceScan.result_count), 0),
                func.count(SourceScan.id).filter(completed_read),
                func.count(SourceScan.id).filter(SourceScan.status == "incomplete"),
                func.count(SourceScan.id).filter(SourceScan.status == "failed"),
                func.min(SourceScan.scanned_on).filter(completed_read),
                func.max(SourceScan.scanned_on).filter(SourceScan.raw_count > 0),
            ).where(SourceScan.source_id == source.id)
        ).one()
        scan_count, staged_count, read_count, incomplete_count, failed_count = row[:5]
        first_read_on, last_listed_on = row[5:]
        # A source saved before its platform's adapter existed still stores no platform.
        platform = _known_platform(source.resolved_platform) or _url_platform(source.url)
        return SourceResponse(
            id=source.id,
            name=source.name,
            employer=None if employer is None else employer.name,
            url=source.url,
            careers_url=None if employer is None else employer.careers_url,
            source_type=SourceKind(source.source_type),
            platform=platform,
            priority=source.priority,
            enabled=source.enabled,
            terms=SourceTerms(url=source.terms_url, condition=source.terms_condition),
            last_scanned_on=None if latest is None else latest.scanned_on,
            last_status=None if latest is None else latest.status,  # type: ignore[arg-type]
            last_read_outcome=None if latest is None else SourceReadOutcome(latest.read_outcome),
            last_notes="" if latest is None else latest.notes,
            scan_count=scan_count,
            staged_count=staged_count,
            read_scan_count=read_count,
            incomplete_scan_count=incomplete_count,
            failed_scan_count=failed_count,
            first_read_on=first_read_on,
            last_listed_on=last_listed_on,
        )

    def remember_board(
        self,
        workspace_id: str,
        employer_name: str,
        board_url: str,
        *,
        enabled: bool = True,
        origin: str,
        correlation_id: str | None = None,
    ) -> str:
        """Persist one already-verified board as internal sweep state."""
        with self.database.session() as session:
            return self.remember_board_in_session(
                session,
                workspace_id,
                employer_name,
                board_url,
                enabled=enabled,
                origin=origin,
                correlation_id=correlation_id,
            )

    def remember_board_in_session(
        self,
        session: Session,
        workspace_id: str,
        employer_name: str,
        board_url: str,
        *,
        enabled: bool = True,
        origin: str,
        correlation_id: str | None = None,
    ) -> str:
        """Persist one board address inside the caller's transaction."""
        name = employer_name.strip()[:300]
        url = canonicalise_source_url(board_url)
        normalized = normalize_text(name)
        employer = self._existing_employer(session, workspace_id, normalized)
        if employer is None:
            employer = self._create_employer(
                session,
                workspace_id=workspace_id,
                name=name,
                normalized_name=normalized,
                origin=origin,
                correlation_id=correlation_id,
            )
        else:
            employer.name = name
            employer.archived_at = None
        self._offer_careers_page(employer, url)
        held = list(
            session.scalars(
                select(Source).where(
                    Source.workspace_id == workspace_id,
                    Source.employer_id == employer.id,
                )
            )
        )
        identity = board_identity_key(url)
        # A row reading this board wins over the employer's other boards.
        source = next(
            (existing for existing in held if _reads_board(existing, identity)),
            held[0] if held else None,
        )
        if source is None:
            source = Source(
                workspace_id=workspace_id,
                employer_id=employer.id,
                name=name,
                source_type=SourceKind.EMPLOYER_BOARD.value,
                url=url,
                resolved_platform=_url_platform(url),
                priority=1,
                enabled=enabled,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.add(source)
            session.flush()
        else:
            # Source names are unique, so a sibling board keeps its own name.
            if all(other.name != name for other in held if other is not source):
                source.name = name
            source.url = url
            source.enabled = enabled
            source.archived_at = None
            # The stored platform outlives an address change only while a saved
            # resolution, not the new address, is what a scan reads.
            platform = _url_platform(url)
            if platform is not None or source.resolved_url is None:
                source.resolved_platform = platform
        return source.id

    def remember_guided_board_in_session(
        self,
        session: Session,
        workspace_id: str,
        employer_name: str,
        board_url: str,
        *,
        enabled: bool,
        correlation_id: str | None = None,
    ) -> str | None:
        """Add a model-proposed source without changing a person-maintained source."""
        name = employer_name.strip()[:300]
        url = canonicalise_source_url(board_url)
        normalized = normalize_text(name)
        employer = self._existing_employer(session, workspace_id, normalized)
        if employer is not None and employer.archived_at is not None:
            return None
        if employer is None:
            employer = self._create_employer(
                session,
                workspace_id=workspace_id,
                name=name,
                normalized_name=normalized,
                origin="search.guided_campaign",
                correlation_id=correlation_id,
            )
        identity = board_identity_key(url)
        for existing in session.scalars(
            select(Source).where(
                Source.workspace_id == workspace_id,
                Source.employer_id == employer.id,
            )
        ):
            if _reads_board(existing, identity):
                return existing.id if existing.archived_at is None else None
        self._offer_careers_page(employer, url)
        source_name = name
        suffix = 0
        while (
            session.scalar(
                select(Source.id).where(
                    Source.workspace_id == workspace_id,
                    Source.name == source_name,
                )
            )
            is not None
        ):
            suffix += 1
            marker = f" (guided {suffix})"
            source_name = f"{name[: 300 - len(marker)]}{marker}"
        source = Source(
            workspace_id=workspace_id,
            employer_id=employer.id,
            name=source_name,
            source_type=SourceKind.EMPLOYER_BOARD.value,
            url=url,
            resolved_platform=_url_platform(url),
            priority=1,
            enabled=enabled,
            origin="search.guided_campaign",
            correlation_id=correlation_id,
        )
        session.add(source)
        session.flush()
        return source.id

    @staticmethod
    def _offer_careers_page(employer: Employer, url: str) -> None:
        """Fill an empty careers page from an address that is the employer's own site."""
        if employer.careers_url is None and not job_board_address(url):
            employer.careers_url = url

    @staticmethod
    def _existing_employer(
        session: Session, workspace_id: str, normalized_name: str
    ) -> Employer | None:
        """Return the normalized employer after validating the active workspace."""
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or workspace.archived_at is not None:
            raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
        return session.scalar(
            select(Employer).where(
                Employer.workspace_id == workspace_id,
                Employer.normalized_name == normalized_name,
            )
        )

    @staticmethod
    def _create_employer(
        session: Session,
        *,
        workspace_id: str,
        name: str,
        normalized_name: str,
        origin: str,
        correlation_id: str | None,
    ) -> Employer:
        """Create the employer identity shared by its source rows."""
        employer = Employer(
            workspace_id=workspace_id,
            name=name,
            normalized_name=normalized_name,
            origin=origin,
            correlation_id=correlation_id,
        )
        session.add(employer)
        session.flush()
        return employer
