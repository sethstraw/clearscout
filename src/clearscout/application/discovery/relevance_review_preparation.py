"""Prepare readable campaign discoveries for automatic relevance review."""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from sqlalchemy import select

from clearscout.application.config import Settings
from clearscout.application.discovery.content_resolution import CurrentPostingContentReader
from clearscout.application.discovery.derived_vocabulary_rules import searched_titles
from clearscout.application.discovery.dispositions import DiscoveryDispositionService
from clearscout.application.discovery.pending import (
    PendingMarkers,
    family_markers,
    source_employer_names,
)
from clearscout.application.discovery.posting_fetch import PostingFetcher
from clearscout.application.discovery.proposals import (
    accepted_archetypes,
    accepted_titles,
    widened_focus,
)
from clearscout.application.discovery.relevance_review_records import (
    RelevanceReviewContract,
    RelevanceReviewPostingInput,
    current_review_in,
    keep_note_digest,
    relevance_review_digest,
)
from clearscout.application.discovery.screening import (
    ScreeningFinding,
    ScreeningStanding,
    screen_discovery,
)
from clearscout.application.discovery.search_parameter_summary import search_summary
from clearscout.application.discovery.search_planning_queries import SearchPlanningQueries
from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.fit import Gate, Standing, gates, read, refusal
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.staged_facts import StagedListingFacts, vouched_identity
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    PendingDiscovery,
    RelevanceReview,
)
from clearscout.infrastructure.web_fetch.ingest import strip_injected_instructions
from clearscout.infrastructure.web_fetch.policy import UNRESOLVED_DESTINATION_NOTE
from clearscout.infrastructure.web_fetch.posting_fetch import UNRESOLVED_DESTINATION_METHOD
from clearscout.shared.errors import IngestError

_MAX_POSTING_CHARACTERS = 30_000

RelevanceReviewPreparationOutcome = Literal[
    "readable",
    "regional_variant",
    "deterministic_refuse",
    "gate_refuse",
    "gate_protected",
    "closed",
    "fetch_refused",
    "fetch_failed",
    "not_retained",
    "posting_too_large",
    "no_requirements",
    "too_short",
    "already_reviewed",
]


@dataclass(frozen=True)
class RelevanceReviewPreparationItem:
    """One candidate's deterministic preparation outcome."""

    pending_discovery_id: str
    employer: str
    title: str
    outcome: RelevanceReviewPreparationOutcome
    detail: str
    refused_gate: str = ""
    # Conditions this posting fails that a person's own decision keeps a rule from refusing.
    protected_conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedRelevanceReview:
    """All readable candidates and the evidence lineage used to review them."""

    pending: list[PendingDiscovery]
    inputs: dict[str, RelevanceReviewPostingInput]
    parameters: JobSearchParameters
    settings: Settings
    inventory: CvInventory
    master_cv_version_id: str
    search_parameter_set_id: str
    search_context: str
    # The person's own note on each readable row whose family they kept with one.
    notes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _ReviewInputs:
    """The workspace-wide inputs a stored review records alongside each posting's own."""

    contract: RelevanceReviewContract
    master_cv_version_id: str
    search_parameter_set_id: str
    search_context: str


def _kept_past(protected: tuple[str, ...]) -> str:
    """Return the detail a row carries when a person's decision keeps a rule from refusing."""
    return (
        f"Your own decision stands on {', '.join(protected)}, "
        "so the rule did not refuse this posting again."
    )


def _found_too_short(review: RelevanceReview, body: str, contract: RelevanceReviewContract) -> bool:
    """Report whether a review, on this prompt, found this exact text too short."""
    return (
        review.recommendation is None
        and review.body_sha256 == relevance_review_digest(body)
        and review.instructions_digest == contract.instructions_digest
        and review.schema_digest == contract.schema_digest
    )


def _reviewed_from(
    review: RelevanceReview,
    row: PendingDiscovery,
    body: str,
    note: str | None,
    current: _ReviewInputs,
) -> bool:
    """Report whether a review was sent every input this row would be sent now."""
    # The model is not an input here: a review from another model still counts, and only a
    # prompt or schema change sends the row again.
    return (
        review.body_sha256 == relevance_review_digest(body)
        and review.source_url_sha256
        == relevance_review_digest(row.canonical_url or row.discovery_url)
        and review.keep_note_sha256 == keep_note_digest(note)
        and review.master_cv_version_id == current.master_cv_version_id
        and review.search_parameter_set_id == current.search_parameter_set_id
        and review.search_context_sha256 == relevance_review_digest(current.search_context)
        and review.instructions_digest == current.contract.instructions_digest
        and review.schema_digest == current.contract.schema_digest
    )


class RelevanceReviewPreparationService:
    """Read and screen every campaign candidate before provider access."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        fetcher: PostingFetcher,
    ) -> None:
        """Bind workspace evidence, deterministic screening, and the posting reader."""
        self.database = database
        self.queries = SearchPlanningQueries(database, storage)
        self.fetcher = fetcher
        self.current_content = CurrentPostingContentReader(database, storage=storage)

    def prepare(
        self,
        workspace_id: str,
        ids: list[str],
        *,
        contract: RelevanceReviewContract,
        require_retained: bool = False,
    ) -> tuple[PreparedRelevanceReview, list[RelevanceReviewPreparationItem]]:
        """Return every readable candidate and an outcome for every requested id."""
        planning = self.queries.planning_inputs(workspace_id)
        settings, inventory = planning.settings, planning.inventory
        master_cv_version_id = planning.master_cv_version_id
        parameter_set_id = planning.search_parameter_set_id
        parameters = settings.job_search.model_copy(
            update={"focus": widened_focus(self.database, workspace_id, settings.job_search.focus)}
        )
        search_context = search_summary(
            parameters, accepted_archetypes(self.database, workspace_id)
        )
        pending = self._pending(workspace_id, ids)
        searched = self._searched_titles(workspace_id, parameters)
        employers = self._source_employers(pending)
        markers = self._markers(workspace_id, pending)
        readable: list[PendingDiscovery] = []
        inputs: dict[str, RelevanceReviewPostingInput] = {}
        notes: dict[str, str] = {}
        outcomes: list[RelevanceReviewPreparationItem] = []
        current = _ReviewInputs(contract, master_cv_version_id, parameter_set_id, search_context)
        in_run = {row.id for row in pending}
        for row in pending:
            note = markers[row.id].review_note
            outcome, posting_input = self._read(
                row,
                parameters,
                searched,
                employers,
                inventory,
                marked=markers[row.id].marked,
                note=note,
                current=current,
                require_retained=require_retained,
                in_run=in_run,
            )
            outcomes.append(outcome)
            if posting_input is not None:
                readable.append(row)
                inputs[row.id] = posting_input
                if note is not None:
                    notes[row.id] = note
        return (
            PreparedRelevanceReview(
                readable,
                inputs,
                parameters,
                settings,
                inventory,
                master_cv_version_id,
                parameter_set_id,
                search_context,
                notes,
            ),
            outcomes,
        )

    def content_candidate_ids(self, workspace_id: str, ids: list[str]) -> list[str]:
        """Return selected rows that are not already deterministically refused."""
        settings, _inventory, _master_cv_version_id = self.queries.inputs(workspace_id)
        parameters = settings.job_search.model_copy(
            update={"focus": widened_focus(self.database, workspace_id, settings.job_search.focus)}
        )
        searched = self._searched_titles(workspace_id, parameters)
        pending = self._pending(workspace_id, ids)
        employers = self._source_employers(pending)
        markers = self._markers(workspace_id, pending)
        return [
            row.id
            for row in pending
            if self._screened(row, parameters, searched, employers, marked=markers[row.id].marked)
            is not None
        ]

    def _read(
        self,
        row: PendingDiscovery,
        parameters: JobSearchParameters,
        searched: list[str],
        employers: dict[str, str],
        inventory: CvInventory,
        *,
        marked: bool,
        note: str | None,
        current: _ReviewInputs,
        require_retained: bool,
        in_run: Collection[str],
    ) -> tuple[RelevanceReviewPreparationItem, RelevanceReviewPostingInput | None]:
        if row.regional_variant_of is not None and self._head_carries_review(
            row.workspace_id,
            row.regional_variant_of,
            parameters,
            searched,
            employers,
            marked=marked,
            in_run=in_run,
        ):
            return self._item(
                row,
                "regional_variant",
                "The head of this regional family carries the review for this opening.",
            ), None
        held = self._screened(row, parameters, searched, employers, marked=marked)
        if held is None:
            return self._item(
                row, "deterministic_refuse", "Already refused by current screening rules."
            ), None
        retained = self.current_content.read(row.workspace_id, row.id)
        if retained is not None:
            if not retained.body.strip():
                return self._item(
                    row,
                    "closed",
                    "The retained content has no usable posting body.",
                ), None
            if self._judged_without_requirements(row, retained.body):
                return self._item(
                    row,
                    "no_requirements",
                    "An evaluation found no requirements in this retained body.",
                ), None
            posting = RelevanceReviewPostingInput(
                retained.body.decode("utf-8"), retained.fetched_at
            )
        elif require_retained:
            return self._item(
                row,
                "not_retained",
                "No verified publisher body is available for relevance review.",
            ), None
        else:
            fetched = self._fetched(row)
            if isinstance(fetched, RelevanceReviewPreparationItem):
                return fetched, None
            posting = fetched
        # A condition a rule can read is never sent for a paid opinion.
        refused, protected = self._refusing_gate(
            row, posting.body, parameters, inventory, marked=marked
        )
        if refused is not None:
            return replace(
                self._item(row, "gate_refuse", refused.reason), refused_gate=refused.name
            ), None
        # Stored evidence stays as published; only the text a model reads is sanitized.
        posting = replace(posting, body=strip_injected_instructions(posting.body))
        if withheld := self._held_back(row, posting.body, note, current):
            return withheld, None
        return self._ready(row, tuple(dict.fromkeys((*held, *protected)))), posting

    def _held_back(
        self, row: PendingDiscovery, body: str, note: str | None, current: _ReviewInputs
    ) -> RelevanceReviewPreparationItem | None:
        """Say why the text a model would read is not worth a paid review, if it is not."""
        with self.database.session() as session:
            review = current_review_in(session, row.workspace_id, row.id)
        if review is not None and _found_too_short(review, body, current.contract):
            return self._item(
                row, "too_short", "A review already found this exact text too short to judge."
            )
        if review is not None and _reviewed_from(review, row, body, note, current):
            return self._item(
                row, "already_reviewed", "The review in force was sent these exact inputs."
            )
        if len(body) > _MAX_POSTING_CHARACTERS:
            return self._item(
                row,
                "posting_too_large",
                "The posting exceeds the bounded relevance-review input ceiling.",
            )
        return None

    def _ready(
        self, row: PendingDiscovery, protected: tuple[str, ...]
    ) -> RelevanceReviewPreparationItem:
        """Say a row is ready, naming any condition a person's own decision held a gate off."""
        if not protected:
            return self._item(row, "readable", "Ready for automatic relevance review.")
        return replace(
            self._item(row, "gate_protected", _kept_past(protected)),
            protected_conditions=protected,
        )

    def _fetched(
        self, row: PendingDiscovery
    ) -> RelevanceReviewPreparationItem | RelevanceReviewPostingInput:
        """Read a posting nobody retained, or say why it could not be read."""
        try:
            fetched = self.fetcher.fetch(row.canonical_url or row.discovery_url)
        except ValidationError as error:
            return self._item(row, "fetch_refused", str(error))
        except IngestError:
            return self._item(row, "fetch_failed", "The posting could not be read safely.")
        # A host that answered no address is retryable, not a posting that closed.
        if fetched.method == UNRESOLVED_DESTINATION_METHOD:
            return self._item(row, "fetch_failed", UNRESOLVED_DESTINATION_NOTE)
        if fetched.posting_status != "active" or not fetched.text or not fetched.text.strip():
            return self._item(
                row, "closed", "The source no longer returned an active posting body."
            )
        return RelevanceReviewPostingInput(fetched.text, fetched.fetched_at)

    def _judged_without_requirements(self, row: PendingDiscovery, body: bytes) -> bool:
        """Report whether a standing dismissal found no requirements in this exact body."""
        with self.database.session() as session:
            judged = DiscoveryDispositionService(self.database).no_requirements_body_in_session(
                session, row.workspace_id, row.id
            )
        return judged == hashlib.sha256(body).hexdigest()

    def _refusing_gate(
        self,
        row: PendingDiscovery,
        text: str,
        parameters: JobSearchParameters,
        inventory: CvInventory,
        *,
        marked: bool,
    ) -> tuple[Gate | None, tuple[str, ...]]:
        """Return the rule gate this posting fails and the conditions a person kept it past.

        A favorite or kept family, or the person's own restore of a gate, holds the row past it.
        """
        title, employer = vouched_identity(
            StagedListingFacts.from_json(row.staged_facts).publisher_vouched,
            row.title,
            row.employer,
        )
        found = gates(
            read(text, inventory, parameters=parameters, listing_location=row.location),
            parameters,
            title=title,
            employer=employer,
        )
        if refusal(found) is None:
            return None, ()
        if marked:
            return None, tuple(gate.name for gate in found if gate.standing is Standing.INELIGIBLE)
        dispositions = DiscoveryDispositionService(self.database)
        with self.database.session() as session:
            restored = tuple(
                gate.name
                for gate in found
                if gate.standing is Standing.INELIGIBLE
                and dispositions.person_restored_in_session(
                    session, row.workspace_id, row.id, gate.name
                )
            )
        return refusal(tuple(gate for gate in found if gate.name not in restored)), restored

    def _head_carries_review(
        self,
        workspace_id: str,
        head_id: str,
        parameters: JobSearchParameters,
        searched: list[str],
        employers: dict[str, str],
        *,
        marked: bool,
        in_run: Collection[str],
    ) -> bool:
        """Report whether a row's family head, unrefused, is in this run or already reviewed.

        The head was chosen when its body was read, so a person who has narrowed
        their search since then is otherwise left with a family nothing reviews.
        """
        with self.database.session() as session:
            head = session.get(PendingDiscovery, head_id)
            if head is None or head.workspace_id != workspace_id:
                return True
            reviewed = current_review_in(session, workspace_id, head_id) is not None
            session.expunge(head)
        if head_id not in in_run and not reviewed:
            return False
        return self._screened(head, parameters, searched, employers, marked=marked) is not None

    def _source_employers(self, rows: list[PendingDiscovery]) -> dict[str, str]:
        """Return the registered employer of each source these rows or their family heads cite."""
        head_ids = [row.regional_variant_of for row in rows if row.regional_variant_of]
        with self.database.session() as session:
            head_sources = (
                list(
                    session.scalars(
                        select(PendingDiscovery.source_id).where(PendingDiscovery.id.in_(head_ids))
                    )
                )
                if head_ids
                else []
            )
            return source_employer_names(session, [*(row.source_id for row in rows), *head_sources])

    def _markers(
        self, workspace_id: str, rows: list[PendingDiscovery]
    ) -> dict[str, PendingMarkers]:
        """Return the markers of the family each row belongs to."""
        with self.database.session() as session:
            found = family_markers(session, workspace_id, [row.id for row in rows])
        return {row.id: found.get(row.id, PendingMarkers()) for row in rows}

    def _screened(
        self,
        row: PendingDiscovery,
        parameters: JobSearchParameters,
        searched: list[str],
        employers: dict[str, str],
        *,
        marked: bool,
    ) -> tuple[str, ...] | None:
        """Return None when screening refuses a row, else the dimensions a person held it past.

        A marked family, or the person's own restore of every refusing code, holds the row.
        """
        screening = screen_discovery(
            row.employer,
            row.title,
            row.location,
            parameters,
            searched,
            StagedListingFacts.from_json(row.staged_facts),
            source_employer=employers.get(row.source_id or "", ""),
        )
        refusing = [
            finding
            for finding in screening.findings
            if finding.standing is ScreeningStanding.REFUSE
        ]
        if not refusing:
            return ()
        if not marked and not self._restored(row, refusing):
            return None
        return tuple(dict.fromkeys(finding.dimension.value for finding in refusing))

    def _restored(self, row: PendingDiscovery, refusing: list[ScreeningFinding]) -> bool:
        """Report whether the person's own restore stands on every code that refuses the row."""
        dispositions = DiscoveryDispositionService(self.database)
        with self.database.session() as session:
            return all(
                dispositions.person_restored_in_session(
                    session, row.workspace_id, row.id, finding.code.value
                )
                for finding in refusing
            )

    @staticmethod
    def _item(
        row: PendingDiscovery, outcome: RelevanceReviewPreparationOutcome, detail: str
    ) -> RelevanceReviewPreparationItem:
        return RelevanceReviewPreparationItem(
            pending_discovery_id=row.id,
            employer=row.employer,
            title=row.title,
            outcome=outcome,
            detail=" ".join(detail.split())[:500],
        )

    def _pending(self, workspace_id: str, ids: list[str]) -> list[PendingDiscovery]:
        with self.database.session() as session:
            found = list(
                session.scalars(
                    select(PendingDiscovery).where(
                        PendingDiscovery.workspace_id == workspace_id,
                        PendingDiscovery.id.in_(ids),
                        PendingDiscovery.archived_at.is_(None),
                        PendingDiscovery.withdrawn_at.is_(None),
                    )
                )
            )
        by_id = {item.id: item for item in found}
        missing = [item_id for item_id in ids if item_id not in by_id]
        if missing:
            raise NotFoundError(f"Pending discoveries not found: {', '.join(missing)}")
        return [by_id[item_id] for item_id in ids]

    def _searched_titles(self, workspace_id: str, parameters: JobSearchParameters) -> list[str]:
        with self.database.session() as session:
            accepted = accepted_titles(session, workspace_id)
        return [item.title for item in searched_titles(parameters, accepted, ())]
