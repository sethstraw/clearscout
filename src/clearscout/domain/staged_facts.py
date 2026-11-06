"""Publisher-stated facts retained before a posting is fully ingested."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

# Stored documents from_json reads; as_json writes the last.
STAGED_FACTS_VERSIONS = (3, 4, 5)
# Fewer reads than this that each moved the date are a repost, not a rolling date.
_ROLLING_DATE_READS = 3
_READ_DAY_ECHO = "The posted date repeats the day it was read with another year."


class ListingIdentityTrust(StrEnum):
    """Whether staged identity text came from the publisher or is only a search hint."""

    PUBLISHER = "publisher"
    UNVERIFIED_HINT = "unverified_hint"


class FreshnessState(StrEnum):
    """A probe hint on a staged listing; never a filter."""

    UNKNOWN = "unknown"
    VERIFIED_CURRENT = "verified_current"
    LIKELY_STALE = "likely_stale"


@dataclass(frozen=True)
class PublishedFact:
    """Values one publisher field states, and whether that answer is complete."""

    values: tuple[str, ...] = ()
    unresolved_values: tuple[str, ...] = ()
    provenance: str = ""
    complete: bool = False

    def as_json(self) -> dict[str, object]:
        """Return the stable JSON representation stored with a pending discovery."""
        return {
            "values": list(self.values),
            "unresolved_values": list(self.unresolved_values),
            "provenance": self.provenance,
            "complete": self.complete,
        }

    @classmethod
    def from_json(cls, value: object) -> PublishedFact:
        """Reconstruct one fact without trusting arbitrary stored JSON shapes."""
        if not isinstance(value, dict):
            return cls()
        values = value.get("values")
        unresolved = value.get("unresolved_values")
        return cls(
            values=_strings(values),
            unresolved_values=_strings(unresolved),
            provenance=str(value.get("provenance") or ""),
            complete=value.get("complete") is True,
        )


# A board's own statement of when it stops listing a job, unlike a closing date.
LISTED_UNTIL = "listed_until"
# Statement bases read from the publisher's record rather than the posting body.
PUBLISHER_RECORD_BASES = frozenset({"valid_through", LISTED_UNTIL})


@dataclass(frozen=True)
class ClosingDateStatement:
    """One publisher statement, including any unresolved date precision."""

    wording: str
    basis: str
    normalized_value: str | None = None
    precision: str | None = None
    timezone: str | None = None
    clarification: str = ""
    artifact_id: str | None = None


@dataclass(frozen=True)
class PublisherClosingDate:
    """Closing statements retained independently of posting availability."""

    statements: tuple[ClosingDateStatement, ...] = ()
    source_url: str = ""
    artifact_id: str | None = None
    observed_at: datetime | None = None

    @property
    def needs_clarification(self) -> bool:
        """Report unresolved or conflicting publisher statements."""
        values = {
            (item.normalized_value, item.precision, item.timezone) for item in self.statements
        }
        return any(item.clarification for item in self.statements) or len(values) > 1

    @property
    def normalized_value(self) -> str | None:
        """Return a supported date only when all statements agree."""
        if not self.statements or self.needs_clarification:
            return None
        return self.statements[0].normalized_value

    @property
    def precision(self) -> str | None:
        """Keep a calendar day distinct from a timestamp."""
        return self.statements[0].precision if self.normalized_value else None

    @property
    def timezone(self) -> str | None:
        """Return the stated offset without inventing one for a naive timestamp."""
        return self.statements[0].timezone if self.normalized_value else None

    def has_passed(self, at: datetime) -> bool | None:
        """Compare a day or zoned instant without guessing an unstated timezone."""
        value = self.normalized_value
        if value is None:
            return None
        try:
            if self.precision == "date":
                return at.date() > date.fromisoformat(value)
            if self.precision == "timestamp" and self.timezone and at.tzinfo is not None:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo is not None:
                    return at >= stamp
        except ValueError:
            return None
        return None

    @property
    def states_listing_end(self) -> bool:
        """Report whether a board stated when it stops listing the job."""
        return any(item.basis == LISTED_UNTIL for item in self.statements)

    @property
    def from_publisher_record(self) -> bool:
        """Report whether any statement was read from the publisher's record."""
        return any(item.basis in PUBLISHER_RECORD_BASES for item in self.statements)

    def listing_expired(self, at: datetime) -> bool:
        """Return whether a board's stated end of listing has passed; a closing date never does."""
        return self.states_listing_end and self.has_passed(at) is True

    def with_artifacts(
        self, *, body_artifact_id: str | None, publisher_artifact_id: str | None
    ) -> PublisherClosingDate:
        """Bind each statement to its retained body or publisher representation."""
        has_publisher_statement = self.from_publisher_record
        return replace(
            self,
            artifact_id=(publisher_artifact_id or body_artifact_id)
            if not self.statements
            else publisher_artifact_id
            if has_publisher_statement
            else body_artifact_id,
            statements=tuple(
                replace(
                    item,
                    artifact_id=body_artifact_id
                    if item.basis == "body_label"
                    else publisher_artifact_id
                    if item.basis in PUBLISHER_RECORD_BASES
                    else None,
                )
                for item in self.statements
            ),
        )

    def as_json(self) -> dict[str, object]:
        """Return provenance and original statements for durable retention."""
        return {
            "statements": [
                {
                    "wording": item.wording,
                    "basis": item.basis,
                    "normalized_value": item.normalized_value,
                    "precision": item.precision,
                    "timezone": item.timezone,
                    "clarification": item.clarification,
                    "artifact_id": item.artifact_id,
                }
                for item in self.statements
            ],
            "source_url": self.source_url,
            "artifact_id": self.artifact_id,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "normalized_value": self.normalized_value,
            "precision": self.precision,
            "timezone": self.timezone,
            "needs_clarification": self.needs_clarification,
        }

    @classmethod
    def from_json(cls, value: object) -> PublisherClosingDate:
        """Reconstruct retained publisher statements without inventing missing facts."""
        if not isinstance(value, dict):
            return cls()
        raw_statements = value.get("statements")
        statements = []
        if isinstance(raw_statements, list):
            for item in raw_statements:
                if not isinstance(item, dict) or not isinstance(item.get("wording"), str):
                    continue
                statements.append(
                    ClosingDateStatement(
                        wording=item["wording"],
                        basis=str(item.get("basis") or ""),
                        normalized_value=(
                            item["normalized_value"]
                            if isinstance(item.get("normalized_value"), str)
                            else None
                        ),
                        precision=item["precision"]
                        if isinstance(item.get("precision"), str)
                        else None,
                        timezone=item["timezone"]
                        if isinstance(item.get("timezone"), str)
                        else None,
                        clarification=str(item.get("clarification") or ""),
                        artifact_id=item["artifact_id"]
                        if isinstance(item.get("artifact_id"), str)
                        else None,
                    )
                )
        observed_at = None
        if isinstance(value.get("observed_at"), str):
            with suppress(ValueError):
                observed_at = datetime.fromisoformat(value["observed_at"])
        return cls(
            statements=tuple(statements),
            source_url=str(value.get("source_url") or ""),
            artifact_id=value["artifact_id"] if isinstance(value.get("artifact_id"), str) else None,
            observed_at=observed_at,
        )


class PostedDateGrade(StrEnum):
    """Whether a board's posted date means first publication or only the board's own date."""

    FIRST_POSTED = "first_posted"
    BOARD_DATE = "board_date"


@dataclass(frozen=True)
class PostedDateStatement:
    """One publisher posted-date statement and the day it was read."""

    wording: str
    basis: str
    grade: PostedDateGrade
    normalized_value: str | None = None
    precision: str | None = None
    timezone: str | None = None
    clarification: str = ""
    read_on: date | None = None

    @property
    def day(self) -> date | None:
        """Return the UTC calendar day stated; a naive timestamp keeps its own date."""
        if self.normalized_value is None:
            return None
        try:
            if self.precision == "date":
                return date.fromisoformat(self.normalized_value)
            stamp = datetime.fromisoformat(self.normalized_value)
        except ValueError:
            return None
        return stamp.astimezone(UTC).date() if stamp.tzinfo else stamp.date()

    @property
    def usable(self) -> bool:
        """Report a statement that may date the posting."""
        return self.day is not None and not self.clarification

    def as_json(self) -> dict[str, object]:
        """Return the statement as stored."""
        return {
            "wording": self.wording,
            "basis": self.basis,
            "grade": self.grade.value,
            "normalized_value": self.normalized_value,
            "precision": self.precision,
            "timezone": self.timezone,
            "clarification": self.clarification,
            "read_on": self.read_on.isoformat() if self.read_on else None,
        }

    @classmethod
    def from_json(cls, value: object) -> PostedDateStatement | None:
        """Reconstruct a stored statement, or None for a shape that is not one."""
        if not isinstance(value, dict) or not isinstance(value.get("wording"), str):
            return None
        try:
            grade = PostedDateGrade(str(value.get("grade")))
        except ValueError:
            return None
        read_on = None
        if isinstance(value.get("read_on"), str):
            with suppress(ValueError):
                read_on = date.fromisoformat(value["read_on"])
        text = {
            key: value[key] if isinstance(value.get(key), str) else None
            for key in ("normalized_value", "precision", "timezone")
        }
        return cls(
            wording=value["wording"],
            basis=str(value.get("basis") or ""),
            grade=grade,
            clarification=str(value.get("clarification") or ""),
            read_on=read_on,
            **text,
        )


def _preference(statement: PostedDateStatement) -> tuple[bool, bool, date]:
    """Order statements so a usable first-posted date wins, then the earliest day."""
    return (
        not statement.usable,
        statement.grade is not PostedDateGrade.FIRST_POSTED,
        statement.day or date.max,
    )


@dataclass(frozen=True)
class PublisherPostedDate:
    """The earliest posted date a publisher stated for a posting, and its latest statement."""

    earliest: PostedDateStatement
    latest: PostedDateStatement
    # Reads that stated a date, and how many of those stated a different value than before.
    reads: int = 1
    changes: int = 0

    @classmethod
    def stated(cls, statement: PostedDateStatement) -> PublisherPostedDate:
        """Return what one read states."""
        return cls(earliest=statement, latest=statement)

    @property
    def needs_clarification(self) -> bool:
        """Report a kept date that is unreadable, echoes the read day, or rolls on every read."""
        rolling = self.reads >= _ROLLING_DATE_READS and self.changes == self.reads - 1
        return rolling or not self.earliest.usable

    @property
    def day(self) -> date | None:
        """Return the day that may sort and filter the posting, or None."""
        return None if self.needs_clarification else self.earliest.day

    @property
    def reposted(self) -> bool:
        """Report a later usable date stated after the kept one."""
        later, kept = self.latest.day, self.earliest.day
        return self.latest.usable and later is not None and kept is not None and later > kept

    def as_json(self) -> dict[str, object]:
        """Return both statements and the derived reading for durable retention."""
        return {
            "earliest": self.earliest.as_json(),
            "latest": self.latest.as_json(),
            "reads": self.reads,
            "changes": self.changes,
            "day": self.day.isoformat() if self.day else None,
            "grade": self.earliest.grade.value,
            "reposted": self.reposted,
            "needs_clarification": self.needs_clarification,
        }

    @classmethod
    def from_json(cls, value: object) -> PublisherPostedDate | None:
        """Reconstruct a stored posted date, or None when it holds no statement."""
        if not isinstance(value, dict):
            return None
        earliest = PostedDateStatement.from_json(value.get("earliest"))
        if earliest is None:
            return None
        reads, changes = value.get("reads"), value.get("changes")
        return cls(
            earliest=earliest,
            latest=PostedDateStatement.from_json(value.get("latest")) or earliest,
            reads=reads if isinstance(reads, int) and reads > 0 else 1,
            changes=changes if isinstance(changes, int) and changes >= 0 else 0,
        )


def merged_posted_date(
    stored: PublisherPostedDate | None, read: PublisherPostedDate | None, read_on: date
) -> PublisherPostedDate | None:
    """Keep the earliest posted date stated, recording one read's statement as the latest.

    A read stating no date, or restating the latest value on its read day, keeps what is stored.
    """
    if read is None:
        return stored
    statement = replace(read.latest, read_on=read_on)
    day = statement.day
    if day is not None and day.year != read_on.year and f"{day:%m-%d}" == f"{read_on:%m-%d}":
        statement = replace(statement, clarification=statement.clarification or _READ_DAY_ECHO)
    if stored is None:
        return PublisherPostedDate.stated(statement)
    # One pass can read the same fetch twice, through its check and its body.
    if (stored.latest.read_on, stored.latest.normalized_value) == (
        read_on,
        statement.normalized_value,
    ):
        return stored
    changed = statement.normalized_value != stored.latest.normalized_value
    return PublisherPostedDate(
        earliest=min(stored.earliest, statement, key=_preference),
        latest=statement,
        reads=stored.reads + 1,
        changes=stored.changes + changed,
    )


@dataclass(frozen=True)
class StagedListingFacts:
    """Structured screening facts retained from a published listing summary."""

    identity_trust: ListingIdentityTrust = ListingIdentityTrust.PUBLISHER
    employer: PublishedFact = field(default_factory=PublishedFact)
    role_aliases: PublishedFact = field(default_factory=PublishedFact)
    locations: PublishedFact = field(default_factory=PublishedFact)
    arrangements: PublishedFact = field(default_factory=PublishedFact)
    employment_types: PublishedFact = field(default_factory=PublishedFact)
    compensation: PublishedFact = field(default_factory=PublishedFact)
    freshness: FreshnessState = FreshnessState.UNKNOWN
    # Saved focus terms the posting's own summary states, which a rescreen reads as the summary.
    focus_terms: PublishedFact = field(default_factory=PublishedFact)
    closing_date: PublisherClosingDate | None = None
    posted_date: PublisherPostedDate | None = None

    @property
    def publisher_vouched(self) -> bool:
        """Return whether the publisher, not a search hint, supplied the staged identity."""
        return self.identity_trust is ListingIdentityTrust.PUBLISHER

    def as_json(self) -> dict[str, object]:
        """Return a versioned document suitable for durable JSON storage."""
        return {
            "schema_version": STAGED_FACTS_VERSIONS[-1],
            "identity_trust": self.identity_trust.value,
            "employer": self.employer.as_json(),
            "role_aliases": self.role_aliases.as_json(),
            "locations": self.locations.as_json(),
            "arrangements": self.arrangements.as_json(),
            "employment_types": self.employment_types.as_json(),
            "compensation": self.compensation.as_json(),
            "freshness": self.freshness.value,
            "focus_terms": self.focus_terms.as_json(),
            "closing_date": self.closing_date.as_json() if self.closing_date else None,
            "posted_date": self.posted_date.as_json() if self.posted_date else None,
        }

    @classmethod
    def from_json(cls, value: object) -> StagedListingFacts:
        """Reconstruct the supported version of a durable staged-facts document."""
        if not isinstance(value, dict) or value.get("schema_version") not in STAGED_FACTS_VERSIONS:
            return cls(identity_trust=ListingIdentityTrust.UNVERIFIED_HINT)
        raw_identity_trust = value.get("identity_trust")
        if not isinstance(raw_identity_trust, str):
            raw_identity_trust = ListingIdentityTrust.UNVERIFIED_HINT.value
        try:
            identity_trust = ListingIdentityTrust(raw_identity_trust)
        except ValueError:
            identity_trust = ListingIdentityTrust.UNVERIFIED_HINT
        raw_freshness = value.get("freshness")
        try:
            freshness = (
                FreshnessState(raw_freshness)
                if isinstance(raw_freshness, str)
                else FreshnessState.UNKNOWN
            )
        except ValueError:
            freshness = FreshnessState.UNKNOWN
        return cls(
            identity_trust=identity_trust,
            employer=PublishedFact.from_json(value.get("employer")),
            role_aliases=PublishedFact.from_json(value.get("role_aliases")),
            locations=PublishedFact.from_json(value.get("locations")),
            arrangements=PublishedFact.from_json(value.get("arrangements")),
            employment_types=PublishedFact.from_json(value.get("employment_types")),
            compensation=PublishedFact.from_json(value.get("compensation")),
            freshness=freshness,
            focus_terms=PublishedFact.from_json(value.get("focus_terms")),
            closing_date=(
                PublisherClosingDate.from_json(value["closing_date"])
                if isinstance(value.get("closing_date"), dict)
                else None
            ),
            posted_date=PublisherPostedDate.from_json(value.get("posted_date")),
        )


def vouched_identity(publisher_vouched: bool, title: str, employer: str) -> tuple[str, str]:
    """Return the identity pair a rule may judge, blank unless a publisher vouched for it."""
    return (title, employer) if publisher_vouched else ("", "")


def _strings(value: Any) -> tuple[str, ...]:
    """Keep only non-empty strings from one stored list."""
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
