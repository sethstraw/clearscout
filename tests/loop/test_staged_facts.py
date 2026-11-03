"""Staged publisher facts survive a JSON round trip, and only the publisher vouches for identity."""

from datetime import UTC, date, datetime

import pytest

from clearscout.application.discovery.posting_text import publisher_posted_date
from clearscout.domain.staged_facts import (
    ClosingDateStatement,
    ListingIdentityTrust,
    PostedDateGrade,
    PublishedFact,
    PublisherClosingDate,
    PublisherPostedDate,
    StagedListingFacts,
    merged_posted_date,
)


@pytest.mark.parametrize("publisher_artifact_id", ["publisher-source", None])
def test_closing_statements_cite_the_retained_evidence_that_supplied_them(
    publisher_artifact_id: str | None,
) -> None:
    fact = PublisherClosingDate(
        statements=(
            ClosingDateStatement("2026-10-19", "valid_through", "2026-10-19", "date"),
            ClosingDateStatement("Closing date: 2026-10-19", "body_label", "2026-10-19", "date"),
        ),
        source_url="https://example.test/jobs/42",
        observed_at=datetime(2026, 10, 8, 12, tzinfo=UTC),
    )
    retained = fact.with_artifacts(
        body_artifact_id="converted-body",
        publisher_artifact_id=publisher_artifact_id,
    )
    assert retained.statements[0].artifact_id == publisher_artifact_id
    assert retained.statements[1].artifact_id == "converted-body"
    assert retained.observed_at == fact.observed_at
    assert retained.source_url == fact.source_url
    assert PublisherClosingDate.from_json(retained.as_json()) == retained


def test_publisher_closing_date_json_retains_provenance_and_precision() -> None:
    fact = PublisherClosingDate(
        statements=(
            ClosingDateStatement(
                wording="2026-10-19T17:30:00",
                basis="valid_through",
                normalized_value="2026-10-19T17:30:00",
                precision="timestamp",
            ),
        ),
        source_url="https://example.test/jobs/42",
        artifact_id="retained-artifact",
        observed_at=datetime(2026, 10, 8, 12, tzinfo=UTC),
    )

    assert PublisherClosingDate.from_json(fact.as_json()) == fact
    assert PublisherClosingDate.from_json(None).statements == ()


def test_a_read_without_closing_statements_cites_its_new_publisher_evidence() -> None:
    fact = PublisherClosingDate().with_artifacts(
        body_artifact_id="reused-body", publisher_artifact_id="fresh-publisher-source"
    )
    assert fact.artifact_id == "fresh-publisher-source"
    assert fact.normalized_value is None


def test_missing_date_facts_in_version_three_do_not_invent_a_publisher_date() -> None:
    stored = {
        "schema_version": 3,
        "identity_trust": "publisher",
        "locations": PublishedFact(values=("Freedonia",), complete=True).as_json(),
    }

    facts = StagedListingFacts.from_json(stored)

    assert facts.publisher_vouched
    assert facts.locations.values == ("Freedonia",)
    assert facts.closing_date is None


@pytest.mark.parametrize(
    ("value", "precision", "zone", "expected"),
    [
        ("2026-10-07", "date", None, True),
        ("2026-10-08", "date", None, False),
        ("2026-10-09", "date", None, False),
        ("2026-10-08T05:59:00-06:00", "timestamp", "-06:00", True),
        ("2026-10-08T06:01:00-06:00", "timestamp", "-06:00", False),
        ("2026-10-01T12:00:00", "timestamp", None, None),
    ],
)
def test_passed_closing_dates_use_the_supplied_clock_without_inventing_a_timezone(
    value: str, precision: str, zone: str | None, expected: bool | None
) -> None:
    fact = PublisherClosingDate(
        statements=(ClosingDateStatement(value, "valid_through", value, precision, zone),)
    )

    assert fact.has_passed(datetime(2026, 10, 8, 12, tzinfo=UTC)) is expected


def test_staged_facts_round_trip_and_reject_an_unknown_document_version() -> None:
    facts = StagedListingFacts(
        role_aliases=PublishedFact(
            values=("Data Team Lead",),
            provenance="fictional.job_description",
            complete=True,
        ),
        locations=PublishedFact(
            values=("Freedonia",),
            unresolved_values=("2 locations",),
            provenance="fictional.location",
            complete=False,
        ),
        focus_terms=PublishedFact(
            values=("clinical data integration",), provenance="posting.summary", complete=True
        ),
    )

    assert StagedListingFacts.from_json(facts.as_json()) == facts
    assert StagedListingFacts.from_json({"schema_version": 3}).focus_terms == PublishedFact()
    assert StagedListingFacts.from_json({"schema_version": 99}).identity_trust.value == (
        "unverified_hint"
    )
    unsupported = StagedListingFacts.from_json(
        {
            "schema_version": 2,
            "identity_trust": "publisher",
            "locations": facts.locations.as_json(),
        }
    )
    assert unsupported.identity_trust is ListingIdentityTrust.UNVERIFIED_HINT
    assert unsupported.locations == PublishedFact()
    assert unsupported.role_aliases == PublishedFact()


def test_only_publisher_supplied_staged_identity_reads_as_vouched() -> None:
    assert StagedListingFacts(identity_trust=ListingIdentityTrust.PUBLISHER).publisher_vouched
    assert not StagedListingFacts(
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT
    ).publisher_vouched
    assert not StagedListingFacts.from_json({"schema_version": 99}).publisher_vouched


def _posted(
    value: str, grade: PostedDateGrade = PostedDateGrade.FIRST_POSTED
) -> PublisherPostedDate:
    fact = publisher_posted_date(value, basis="first_published", grade=grade)
    assert fact is not None
    return fact


def _reads(*reads: tuple[str | None, date]) -> PublisherPostedDate | None:
    kept = None
    for value, read_on in reads:
        kept = merged_posted_date(kept, _posted(value) if value else None, read_on)
    return kept


@pytest.mark.parametrize("version", [3, 4, 5])
def test_each_supported_staged_facts_version_is_read(version: int) -> None:
    stored = {
        "schema_version": version,
        "identity_trust": "publisher",
        "locations": PublishedFact(values=("Freedonia",), complete=True).as_json(),
    }

    facts = StagedListingFacts.from_json(stored)

    assert facts.publisher_vouched
    assert facts.locations.values == ("Freedonia",)
    assert facts.posted_date is None
    assert facts.as_json()["schema_version"] == 5


def test_a_posted_date_survives_the_staged_facts_round_trip() -> None:
    facts = StagedListingFacts(
        posted_date=_reads(("2026-09-01", date(2026, 9, 3)), ("2026-09-20", date(2026, 9, 21)))
    )

    restored = StagedListingFacts.from_json(facts.as_json())

    assert restored == facts
    assert restored.posted_date is not None
    assert restored.posted_date.latest.read_on == date(2026, 9, 21)


def test_the_earliest_posted_date_is_kept_and_a_later_one_is_a_repost() -> None:
    kept = _reads(("2026-07-29", date(2026, 8, 1)), ("2026-09-29", date(2026, 9, 30)))

    assert kept is not None
    assert kept.day == date(2026, 7, 29)
    assert kept.reposted
    assert kept.latest.normalized_value == "2026-09-29"
    assert kept.latest.read_on == date(2026, 9, 30)


def test_an_earlier_posted_date_replaces_a_later_one_first_read_after_a_repost() -> None:
    kept = _reads(("2026-09-29", date(2026, 9, 30)), ("2026-07-29", date(2026, 10, 1)))

    assert kept is not None
    assert kept.day == date(2026, 7, 29)
    assert not kept.reposted


def test_a_read_stating_no_posted_date_keeps_the_stored_one() -> None:
    stored = _reads(("2026-09-01", date(2026, 9, 3)))

    assert merged_posted_date(stored, None, date(2026, 9, 10)) == stored


def test_a_first_posted_date_outranks_an_earlier_board_date() -> None:
    board = merged_posted_date(
        None, _posted("2026-08-01", PostedDateGrade.BOARD_DATE), date(2026, 9, 3)
    )

    kept = merged_posted_date(board, _posted("2026-09-01"), date(2026, 9, 4))

    assert kept is not None
    assert kept.day == date(2026, 9, 1)
    assert kept.earliest.grade is PostedDateGrade.FIRST_POSTED


def test_a_posted_date_echoing_the_read_day_with_another_year_is_not_sortable() -> None:
    kept = _reads(("2024-10-22T09:15:00Z", date(2026, 10, 22)))

    assert kept is not None
    assert kept.earliest.wording == "2024-10-22T09:15:00Z"
    assert kept.needs_clarification
    assert kept.day is None


def test_an_echoed_read_day_never_displaces_a_real_earlier_posted_date() -> None:
    kept = _reads(("2026-09-01", date(2026, 9, 3)), ("2024-10-22", date(2026, 10, 22)))

    assert kept is not None
    assert kept.day == date(2026, 9, 1)
    assert not kept.reposted


def test_a_posted_date_that_changes_on_every_read_is_not_sortable() -> None:
    kept = _reads(
        ("2026-09-17", date(2026, 9, 18)),
        ("2026-10-16", date(2026, 10, 17)),
        ("2026-10-20", date(2026, 10, 21)),
    )

    assert kept is not None
    assert kept.needs_clarification
    assert kept.day is None


def test_a_posted_date_reposted_between_steady_reads_stays_sortable() -> None:
    kept = _reads(
        ("2026-09-01", date(2026, 9, 2)),
        ("2026-09-01", date(2026, 9, 9)),
        ("2026-09-22", date(2026, 9, 23)),
        ("2026-10-08", date(2026, 10, 9)),
    )

    assert kept is not None
    assert kept.day == date(2026, 9, 1)
    assert kept.reposted
