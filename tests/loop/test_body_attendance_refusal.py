"""A verified body refuses a scanned posting only by a boundary it states or vouches."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select

from clearscout.application.discovery.board_scan import SourceScanHandler
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.source_reading import SourceListing, SourceRead
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    SourceReadOutcome,
    new_id,
    utc_now,
)
from clearscout.domain.identity import normalize_text
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    CountryNames,
    JobSearchParameters,
    Location,
    Seniority,
    WorkingTimeZone,
)
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Employer,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    Source,
)
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.scenarios import titles

CAREERS = "https://careers.example-health.test/"
POSTING = "https://careers.example-health.test/jobs/data-engineer"


class _Pages:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        status = 200 if url in self.pages else 404
        return httpx.Response(
            status, text=self.pages.get(url, ""), request=httpx.Request("GET", url)
        )


class _Boards:
    def __init__(self, listings: list[SourceListing]) -> None:
        self.found = listings

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=self.found,
            complete=True,
            raw_count=len(self.found),
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


SPRINGFIELD = ("Springfield", "Illinois", "US")
TORONTO = ("Toronto", "Ontario", "CA")
KYIV = ("Kyiv", "Kyiv City", "UA")
MILAN = ("Milan", "Lombardy", "IT")
OSAKA = ("Osaka", "Osaka", "JP")

REMOTE_ONLY = Arrangement(
    remote=Acceptance.PREFERRED,
    hybrid=Acceptance.REFUSED,
    on_site=Acceptance.REFUSED,
    maximum_office_days=0,
    maximum_travel_percent=20,
)
HYBRID_ALLOWED = Arrangement(
    remote=Acceptance.PREFERRED,
    hybrid=Acceptance.ACCEPTABLE,
    on_site=Acceptance.REFUSED,
    maximum_travel_percent=20,
)
PHYSICIAN_DEGREE = ("Requires an MD or DO degree", "not held")
# A hiring organization the source does not name leaves the row an unverified hint.
AGGREGATED = "Northwind Analytics"


def _search(
    arrangement: Arrangement = REMOTE_ONLY,
    *,
    countries: tuple[str, ...] = ("United States",),
    seniority: Seniority | None = None,
) -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(key=f"remote_{index}", country=country, arrangement=arrangement)
            for index, country in enumerate(countries)
        ],
        countries=[CountryNames(country=country) for country in countries],
        seniority=seniority or Seniority(target_titles=titles("Data Engineer")),
        excluded_conditions=[PHYSICIAN_DEGREE],
    )


def _declared_listings(
    places: tuple[tuple[str, str, str], ...],
    *,
    title: str = "Data Engineer",
    organization: str = "Example Health",
    telecommute: bool = False,
) -> list[SourceListing]:
    posting = {
        **({"jobLocationType": "TELECOMMUTE"} if telecommute else {}),
        "@type": "JobPosting",
        "title": title,
        "url": POSTING,
        "hiringOrganization": {"name": organization},
        "jobLocation": [
            {
                "@type": "Place",
                "address": {
                    "addressLocality": locality,
                    "addressRegion": region,
                    "addressCountry": country,
                },
            }
            for locality, region, country in places
        ],
    }
    page = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
    read = BoardApiFetcher(client=cast(Any, _Pages({CAREERS: page}))).listings(
        "Example Health", CAREERS
    )
    return read.listings


def _scan(
    database: Database,
    body: bytes,
    key: str,
    places: tuple[tuple[str, str, str], ...] = (SPRINGFIELD,),
) -> list[str]:
    """Return the categories a remote-only United States search, blind to Toronto, dismisses."""
    return [category for category, _reason in _dismissals(database, body, key, places)]


def _dismissals(
    database: Database,
    body: bytes,
    key: str,
    places: tuple[tuple[str, str, str], ...] = (SPRINGFIELD,),
    *,
    search: JobSearchParameters | None = None,
    title: str = "Data Engineer",
    organization: str = "Example Health",
    telecommute: bool = False,
) -> list[tuple[str, str]]:
    """Scan one declared posting whose verified body is `body`; return its dismissals."""
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug=key, name=key).id
    )
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Example Health",
            normalized_name=normalize_text("Example Health"),
            careers_url=CAREERS,
        )
        session.add(employer)
        session.flush()
        session.add(
            Source(
                id=new_id(),
                workspace_id=workspace_id,
                employer_id=employer.id,
                name="Example Health",
                source_type="employer_board",
                url=CAREERS,
                priority=1,
                enabled=True,
            )
        )
    SearchParametersService(database).save(workspace_id, search or _search())

    class _Body:
        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=body,
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            )

    boards = _Boards(
        _declared_listings(places, title=title, organization=organization, telecommute=telecommute)
    )
    handler = SourceScanHandler(
        database,
        storage=runtime_storage(database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            database, _Body(), storage=runtime_storage(database)
        ),
    )
    queue = TaskQueueService(database)
    task = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=key
    )
    handlers = default_handlers(database, fetcher=boards)
    handlers["source.scan"] = handler
    assert Worker(database, worker_id=key, handlers=handlers).run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    with database.session() as session:
        return [
            (category, reason)
            for category, reason in session.execute(
                select(
                    PendingDiscoveryDispositionEvent.category,
                    PendingDiscoveryDispositionEvent.reason,
                )
                .join(
                    PendingDiscovery,
                    PendingDiscoveryDispositionEvent.pending_discovery_id == PendingDiscovery.id,
                )
                .where(PendingDiscovery.workspace_id == workspace_id)
            )
        ]


def test_a_hybrid_line_refuses_a_completely_placed_declared_posting(
    durable_database: Database,
) -> None:
    body = b"Hybrid Working: Minimum 3 days per week in the office."

    assert _scan(durable_database, body, "hybrid-line") == ["arrangement_refused"]


@pytest.mark.parametrize(
    "remote", [" #LI-Remote", " A remote option is available.", " Work Anywhere."]
)
def test_an_explicit_remote_option_keeps_the_same_posting(
    durable_database: Database, remote: str
) -> None:
    body = b"Hybrid Working: Minimum 3 days per week in the office." + remote.encode()

    assert _scan(durable_database, body, "remote-option") == []


def test_one_body_location_line_does_not_complete_a_multi_place_posting(
    durable_database: Database,
) -> None:
    body = (
        b"Location: Springfield, Illinois, United States\n"
        b"Hybrid Working: Minimum 3 days per week in the office."
    )

    assert _scan(durable_database, body, "multi-place", places=(SPRINGFIELD, TORONTO)) == []


_REFUSED = "arrangement_refused"


@pytest.mark.parametrize(
    ("body", "category", "quoted"),
    [
        (b"4 days onsite per week required.", _REFUSED, "4 days onsite per week required"),
        (b"Work model/flexibility: Onsite", _REFUSED, "Onsite"),
        (b"8 days per month in office.", _REFUSED, "8 days per month in office"),
        (b"You will spend 60% of your time on-site.", _REFUSED, "60% of your time on-site"),
        (
            b"This role will not have the ability to be located remotely.",
            _REFUSED,
            "located remotely",
        ),
        (
            b"This is an on-site role in our Springfield office.\n\nBenefits\n"
            b"- A remote-friendly culture",
            _REFUSED,
            "on-site role",
        ),
        (
            b"This role is on-site. Telecommuting permitted up to 1 day per week.",
            _REFUSED,
            "on-site",
        ),
        (
            b"Location: Remote, United States\n4 days onsite per week required.",
            "arrangement_office_limit",
            "4 days onsite per week required",
        ),
        (
            b"Northwind Health builds remote patient monitoring offerings across acute and "
            b"remote care settings.\nWe're working a minimum of 4 days a week onsite.",
            _REFUSED,
            "4 days a week onsite",
        ),
    ],
)
def test_stated_office_attendance_refuses_a_remote_only_search(
    durable_database: Database, body: bytes, category: str, quoted: str
) -> None:
    [(dismissed, reason)] = _dismissals(durable_database, body, "attendance")

    assert dismissed == category
    assert quoted in reason


REMOTE_US = ("Remote", "", "US")
REMOTE_USA = ("Remote - USA", "", "US")


@pytest.mark.parametrize(
    ("body", "places"),
    [
        (b"This is a hybrid role in our Springfield office.", (SPRINGFIELD, REMOTE_US)),
        (b"This is a hybrid role in our Springfield office.", (REMOTE_USA,)),
        (
            b"This is a hybrid role based in our Springfield office, "
            b"or remotely within the United States.",
            (SPRINGFIELD,),
        ),
        (
            b"This is a hybrid role, based in any of our US offices or remotely within the US.",
            (SPRINGFIELD,),
        ),
        (
            b"Remote work is not available outside the United States; "
            b"this is a remote role within the US.",
            (SPRINGFIELD,),
        ),
        (
            b"This is a fully remote role. "
            b"You will not be able to work remotely from outside the United States.",
            (SPRINGFIELD,),
        ),
    ],
    ids=[
        "remote place listed",
        "remote listing place",
        "or remotely",
        "offices or remotely",
        "not outside the country",
        "not from outside the country",
    ],
)
def test_a_remote_option_the_listing_or_body_states_keeps_the_posting(
    durable_database: Database, body: bytes, places: tuple[tuple[str, str, str], ...]
) -> None:
    assert _dismissals(durable_database, body, "remote-option", places) == []


@pytest.mark.parametrize(
    ("body", "search"),
    [
        (b"This is a fully remote role.", None),
        (b"This is a hybrid role with 2 days per week in the office.", _search(HYBRID_ALLOWED)),
        (b"This is a fully remote role. Up to 15% travel.", None),
        (b"This is a fully remote role with occasional on-site customer visits.", None),
        (
            b"This is a fully remote role.\nRequirements\n- 5 years of experience\n\n"
            b"Preferred\n- MD preferred",
            None,
        ),
    ],
    ids=["remote", "hybrid allowed", "light travel", "customer visits", "degree preferred"],
)
def test_a_body_inside_every_stated_boundary_stays(
    durable_database: Database, body: bytes, search: JobSearchParameters | None
) -> None:
    assert _dismissals(durable_database, body, "inside", search=search) == []


@pytest.mark.parametrize(
    "travel", ["This role requires 40-50% travel.", "Travel up to 25%.", "Travel of 21%."]
)
def test_travel_over_the_limit_refuses_the_posting(durable_database: Database, travel: str) -> None:
    body = f"This is a fully remote role.\n{travel}".encode()

    [(category, reason)] = _dismissals(durable_database, body, "travel")

    assert category == "travel_over_limit"
    assert travel.rstrip(".") in reason


_REMOTE_UNLESS_NEAR = (
    "Role may be conducted remotely unless you live within 100 miles of "
    "our Headquarters in Springfield, Illinois or our second site in Shelbyville, Illinois. "
    "If you do then role reverts to hybrid requiring an onsite presence 2x weekly."
)


def test_remote_offered_to_anyone_living_away_from_the_office_keeps_the_posting(
    durable_database: Database,
) -> None:
    assert _dismissals(durable_database, _REMOTE_UNLESS_NEAR.encode(), "remote-unless") == []


@pytest.mark.parametrize(
    ("index", "condition"),
    list(
        enumerate(
            [
                "unless you live within commuting distance of our office",
                "unless you live within a 50-mile radius of our office",
                "unless you live in the greater Springfield area",
                "unless you live in springfield",
                "unless you live in the state of Colorado",
                "if you live outside commuting distance of our office",
                "if you live beyond commuting distance of our office",
                "if you live outside our hub cities",
                "if you live outside illinois",
                "if you live outside Illinois but within the United States",
                "if you live more than a commutable distance from our office",
                "if you live outside the state of Colorado",
                "if you live outside of the continental US",
                "if you live outside Colorado, but in-person onboarding is required",
                "if you live outside Colorado, but in person onboarding is required",
                "if you live outside Colorado, but in that case you must travel quarterly",
                "if you live outside Colorado but in most cases quarterly travel is expected",
                "if you live outside Colorado but within the contiguous United States",
                "if you live outside Colorado but within the lower 48 states",
                "if you live outside Colorado but within the 50 states",
                "if you live outside the Denver area but within the Mountain or Central time zone",
                "unless you live in a state where we have an office",
            ]
        )
    ),
)
def test_remote_offered_beside_a_local_hybrid_tail_keeps_the_posting(
    durable_database: Database, index: int, condition: str
) -> None:
    body = (
        f"Role may be conducted remotely {condition}. "
        "If you do then role reverts to hybrid requiring an onsite presence 2x weekly."
    )

    assert _dismissals(durable_database, body.encode(), f"remote-tail-{index}") == []


def test_remote_offered_with_no_tail_beside_in_person_onboarding_keeps_the_posting(
    durable_database: Database,
) -> None:
    body = b"Role may be conducted remotely if you live outside Colorado, but in person onboarding."

    assert _dismissals(durable_database, body, "remote-onboarding") == []


@pytest.mark.parametrize(
    ("place", "country", "condition"),
    [
        (TORONTO, "Canada", "if you live outside Ontario but within Canada"),
        (TORONTO, "Canada", "if you live outside the Toronto area but within Canada"),
        (
            ("London", "England", "GB"),
            "United Kingdom",
            "if you live outside London but within the UK",
        ),
    ],
)
def test_remote_offered_within_the_search_country_keeps_the_posting(
    durable_database: Database, place: tuple[str, str, str], country: str, condition: str
) -> None:
    body = (
        f"Role may be conducted remotely {condition}. "
        "If you do then role reverts to hybrid requiring an onsite presence 2x weekly."
    )

    kept = _dismissals(
        durable_database,
        body.encode(),
        "remote-country",
        (place,),
        search=_search(countries=(country,)),
    )

    assert kept == []


def test_remote_offered_to_anyone_living_away_from_the_area_keeps_the_posting(
    durable_database: Database,
) -> None:
    body = (
        b"Role may be performed remotely if you live outside the Springfield area; "
        b"otherwise this is an on-site role."
    )

    assert _dismissals(durable_database, body, "remote-if-away") == []


@pytest.mark.parametrize(
    "body",
    [
        "This position is on-site in Springfield, with the option to work remotely on Fridays.",
        "This role is on-site; telecommuting permitted up to 1 day per week.",
        "The position requires working on site in Springfield 4 days a week, telecommuting 1 day.",
        "Job Description: We are a hybrid company and employees may telecommute two days a "
        "week. This role requires on-site presence 3 days per week.",
        "The role can be done remotely by exception only; the expectation is on-site 5 days "
        "a week.",
        "This position is eligible for telecommuting at the manager's discretion; it is based "
        "on site in Springfield.",
        "This job may be performed remotely after the first 90 days; the first 90 days are "
        "on-site in Springfield, 5 days a week.",
        "Workplace: Hybrid. This role is based in Springfield and may be performed remotely "
        "two days a week.",
        "This position is fully on-site. The job can be performed remotely only during "
        "inclement weather.",
    ],
)
def test_a_remote_sentence_beside_committed_attendance_still_refuses(
    durable_database: Database, body: str
) -> None:
    categories = _scan(durable_database, body.encode(), "remote-beside-attendance")

    assert categories in (["arrangement_refused"], ["arrangement_office_limit"])


def test_living_near_the_office_stated_as_a_rule_still_refuses(
    durable_database: Database,
) -> None:
    body = (
        b"Candidates must live within 50 miles of our Springfield office "
        b"and work on site three days a week."
    )

    assert _scan(durable_database, body, "near-rule") == [_REFUSED]


def test_remote_unless_near_the_office_refuses_on_travel_over_the_limit(
    durable_database: Database,
) -> None:
    body = f"{_REMOTE_UNLESS_NEAR}\nThis role requires up to 40% travel.".encode()

    [(category, reason)] = _dismissals(durable_database, body, "remote-unless-travel")

    assert category == "travel_over_limit"
    assert "up to 40% travel" in reason


@pytest.mark.parametrize(
    "bound",
    [
        "Some travel expected (<25%).",
        "Minimal travel may be required (less than 25%).",
        "Travel under 30% of the time.",
        "Travel below 25%.",
        "Some travel expected (<20%).",
        "Travel under 20% of the time.",
        "Minimal travel may be required (less than 15%).",
    ],
)
def test_travel_stated_only_as_below_a_bound_never_refuses(
    durable_database: Database, bound: str
) -> None:
    body = f"This is a fully remote role.\nRequirements\n- {bound}".encode()

    assert _dismissals(durable_database, body, "travel-bound") == []


def test_a_required_excluded_credential_refuses_the_posting(durable_database: Database) -> None:
    body = b"Requirements\n- MD or DO degree required\n- 5 years of experience"

    [(category, reason)] = _dismissals(durable_database, body, "credential")

    assert category == "condition_excluded"
    assert "MD or DO degree required" in reason


_NO_PLACE = b"This is a fully remote role building pipelines for our analytics team."


@pytest.mark.parametrize("place", [KYIV, MILAN, OSAKA], ids=["ukraine", "italy", "japan"])
def test_a_body_naming_no_place_is_screened_by_the_listing_location(
    durable_database: Database, place: tuple[str, str, str]
) -> None:
    [(category, reason)] = _dismissals(
        durable_database,
        _NO_PLACE,
        "listing-place",
        places=(place,),
        search=_search(countries=("United States", "Canada")),
        organization=AGGREGATED,
    )

    assert category == "location_outside"
    assert place[0] in reason


@pytest.mark.parametrize(
    ("place", "body"),
    [
        (TORONTO, _NO_PLACE),
        (SPRINGFIELD, _NO_PLACE),
        (KYIV, b"Location: Toronto, Ontario, Canada\n" + _NO_PLACE),
    ],
    ids=["canada", "united states", "body place overrides"],
)
def test_a_saved_country_keeps_the_posting(
    durable_database: Database, place: tuple[str, str, str], body: bytes
) -> None:
    dismissals = _dismissals(
        durable_database,
        body,
        "saved-place",
        places=(place,),
        search=_search(countries=("United States", "Canada")),
        organization=AGGREGATED,
    )

    assert dismissals == []


_DIRECTOR_BAND = Seniority(target_titles=titles("Head of Orchard Telemetry"), maximum="director")
_VP_TITLE = "Vice President, Orchard Telemetry"


def test_a_publisher_stated_title_does_not_make_inferred_seniority_a_refusal(
    durable_database: Database,
) -> None:
    body = (
        f"{_VP_TITLE}\n\nThis is a fully remote role leading our orchard telemetry team.".encode()
    )

    dismissals = _dismissals(
        durable_database,
        body,
        "vouched-title",
        search=_search(seniority=_DIRECTOR_BAND),
        title=_VP_TITLE,
        organization=AGGREGATED,
    )

    assert dismissals == []


def test_a_hinted_title_the_page_never_states_stays_unverified(
    durable_database: Database,
) -> None:
    body = b"This is a fully remote role leading our orchard telemetry team."

    dismissals = _dismissals(
        durable_database,
        body,
        "hinted-title",
        search=_search(seniority=_DIRECTOR_BAND),
        title=_VP_TITLE,
        organization=AGGREGATED,
    )

    assert dismissals == []


_TRAVEL_UP_TO_25 = REMOTE_ONLY.model_copy(update={"maximum_travel_percent": 25})


def test_a_requirements_travel_line_inside_the_limit_holds_a_wider_intro_range(
    durable_database: Database,
) -> None:
    body = (
        b"This is a fully remote role. This role may require 25-40% travel.\n\n"
        b"Requirements\n- Ability to travel up to 25%\n- 5 years of experience"
    )

    dismissals = _dismissals(durable_database, body, "two-travel", search=_search(_TRAVEL_UP_TO_25))

    assert dismissals == []


COUNTRY_ONLY = ("", "", "US")


def test_hybrid_naming_no_office_or_day_count_holds_the_posting(
    durable_database: Database,
) -> None:
    body = (
        b"Location: United States\n"
        b"This hybrid role is primarily customer-facing, partnering with clinic teams."
    )

    assert _dismissals(durable_database, body, "hybrid-unplaced", (COUNTRY_ONLY,)) == []


_CENTRAL_HOURS = b"This is a fully remote role. You must work Central time hours."


@pytest.mark.parametrize("shift", [2, None], ids=["saved shift", "no saved shift"])
def test_a_required_time_zone_never_refuses_a_body(
    durable_database: Database, shift: int | None
) -> None:
    update: dict[str, object] = {"working_time_zone": WorkingTimeZone.MOUNTAIN}
    if shift is not None:
        update["working_day_shift_hours"] = shift

    dismissals = _dismissals(
        durable_database, _CENTRAL_HOURS, "zone", search=_search().model_copy(update=update)
    )

    assert dismissals == []


_WINNIPEG_HOME = JobSearchParameters(
    locations=[
        Location(
            key="home",
            country="Canada",
            region="Manitoba",
            metro="Winnipeg",
            resident=True,
            arrangement=Arrangement(
                remote=Acceptance.PREFERRED,
                hybrid=Acceptance.ACCEPTABLE,
                on_site=Acceptance.ACCEPTABLE,
                maximum_travel_percent=20,
            ),
        ),
        Location(key="canada_remote", country="Canada", arrangement=REMOTE_ONLY),
        Location(key="us_remote", country="United States", arrangement=REMOTE_ONLY),
    ],
    countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
    seniority=Seniority(target_titles=titles("Data Engineer")),
)
_SILENT = b"You will build data pipelines for our analytics team using Python and SQL."
_PRESUMED = "no working arrangement stated"


_HELD = [(_REFUSED, True)]


@pytest.mark.parametrize(
    ("place", "body", "telecommute", "expected"),
    [
        pytest.param(("Waltham", "MA", "US"), _SILENT, False, _HELD, id="us-city"),
        pytest.param(("Laval", "Quebec", "CA"), _SILENT, False, _HELD, id="canadian-city"),
        pytest.param(("Waltham Office", "MA", "US"), _SILENT, False, _HELD, id="city-office"),
        pytest.param(("Laval - Plant 3", "Quebec", "CA"), _SILENT, False, _HELD, id="city-site"),
        pytest.param(("Winnipeg", "Manitoba", "CA"), _SILENT, False, [], id="home-metro"),
        pytest.param(COUNTRY_ONLY, _SILENT, False, [], id="country-only"),
        pytest.param(REMOTE_US, _SILENT, False, [], id="remote-place"),
        pytest.param(("", "", ""), _SILENT, False, [], id="no-location"),
        pytest.param(
            ("Waltham", "MA", "US"),
            _SILENT + b" This role is remote.",
            False,
            [],
            id="body-remote",
        ),
        pytest.param(
            ("Waltham", "MA", "US"),
            _SILENT + b" This position is remote-eligible.",
            False,
            [],
            id="body-remote-eligible",
        ),
        pytest.param(("Waltham", "MA", "US"), _SILENT, True, [], id="staged-remote"),
        pytest.param(
            ("Waltham", "MA", "US"),
            _SILENT + b" This is a hybrid role in our Waltham office.",
            False,
            [(_REFUSED, False)],
            id="body-hybrid",
        ),
    ],
)
def test_a_named_office_site_with_no_stated_arrangement_is_presumed_on_site(
    durable_database: Database,
    place: tuple[str, str, str],
    body: bytes,
    telecommute: bool,
    expected: list[tuple[str, bool]],
) -> None:
    """A named site at remote-only places reads as on-site only when nothing states otherwise."""
    dismissals = _dismissals(
        durable_database,
        body,
        "presumed-on-site",
        places=(place,),
        search=_WINNIPEG_HOME,
        telecommute=telecommute,
    )

    assert [(category, _PRESUMED in reason) for category, reason in dismissals] == expected


_WALTHAM = ("Waltham", "MA", "US")


@pytest.mark.parametrize(
    ("title", "place", "body", "expected"),
    [
        *(
            pytest.param(f"Data Engineer {tag}", _WALTHAM, _SILENT, [], id=tag)
            for tag in (
                "- Remote US",
                "(Remote)",
                "- Remote - Canada",
                "- Work from Home",
                "- Virtual",
                "- Telecommute",
            )
        ),
        *(
            pytest.param("Data Engineer", (city, "MO", "US"), _SILENT, [], id=city)
            for city in ("Any city", "Any location", "Multiple cities", "Various")
        ),
        *(
            pytest.param("Data Engineer", _WALTHAM, _SILENT + f" {line}".encode(), [], id=line)
            for line in (
                "We are a fully virtual company, collaborating across 28 countries.",
                "We are a fully remote company.",
                "We are a fully distributed team.",
                "We are a remote company.",
                "Our team is 100% remote.",
            )
        ),
        pytest.param("Data Engineer", _WALTHAM, _SILENT, _HELD, id="office-city"),
        pytest.param(
            "Data Engineer",
            _WALTHAM,
            _SILENT + b"\nBenefits\n- Work-from-Home Stipend",
            _HELD,
            id="home-stipend",
        ),
        pytest.param(
            "Data Engineer",
            _WALTHAM,
            _SILENT + b" We are remote-first with offices in Boston and Denver.",
            _HELD,
            id="remote-first-with-offices",
        ),
        pytest.param(
            "Data Engineer",
            _WALTHAM,
            _SILENT + b" This role is onsite four days per week.",
            [(_REFUSED, False)],
            id="onsite-four-days",
        ),
    ],
)
def test_remote_in_the_title_or_company_keeps_a_named_site(
    durable_database: Database,
    title: str,
    place: tuple[str, str, str],
    body: bytes,
    expected: list[tuple[str, bool]],
) -> None:
    """Remote in the title or company, or a placeholder city, is not presumed on-site."""
    dismissals = _dismissals(
        durable_database, body, "title-remote", places=(place,), search=_WINNIPEG_HOME, title=title
    )

    assert [(category, _PRESUMED in reason) for category, reason in dismissals] == expected


_COUNTRYWIDE = (
    "Telework eligible.",
    "This role is eligible for teleworking.",
    "Telecommute eligible.",
    *(
        f"This role can be {verb} anywhere in {country}."
        for verb in ("based", "located", "performed")
        for country in ("the US", "the U.S.", "the United States", "Canada")
    ),
    "You can work anywhere in the US.",
    "Candidates can be located anywhere in the US.",
    "This position may be performed from any US location.",
    "This position may be performed from any Canadian location.",
    "We are hiring, and this role is open to candidates across the US.",
    "This role is open to candidates across Canada.",
    "We are a remote-first company with offices in Boston and Denver. "
    "This role can be based anywhere in the United States.",
)


@pytest.mark.parametrize("line", _COUNTRYWIDE)
def test_a_countrywide_placement_is_remote_evidence(durable_database: Database, line: str) -> None:
    body = _SILENT + f" {line}".encode()

    dismissals = _dismissals(
        durable_database, body, "countrywide", places=(_WALTHAM,), search=_WINNIPEG_HOME
    )

    assert dismissals == []


@pytest.mark.parametrize(
    "town",
    [
        "Selkirk",
        "Stonewall",
        "Niverville",
        "Springfield",
        "Oakbank",
        "Headingley",
        "Rural Municipality of Ritchot",
    ],
)
@pytest.mark.parametrize("region", ["MB", "Manitoba"])
def test_a_town_in_the_saved_metro_region_is_not_presumed_on_site(
    durable_database: Database, town: str, region: str
) -> None:
    dismissals = _dismissals(
        durable_database,
        _SILENT,
        "metro-town",
        places=((town, region, "CA"),),
        search=_WINNIPEG_HOME,
    )

    assert dismissals == []


@pytest.mark.parametrize(
    "line",
    [
        "Globex is looking for a Staff Data Engineer to help us get closer to executing our "
        "mission: to create access to affordable, quality care across the United States.",
        "We are serving patients across Canada.",
        "We operate clinics across the US.",
        "We have offices across the country.",
        "We support customers anywhere in the US.",
        "Hospitals rely on us across the us health system.",
    ],
)
def test_a_service_or_mission_across_a_country_is_not_remote_evidence(
    durable_database: Database, line: str
) -> None:
    body = _SILENT + f" {line}".encode()

    dismissals = _dismissals(
        durable_database, body, "mission", places=(_WALTHAM,), search=_WINNIPEG_HOME
    )

    assert [(category, _PRESUMED in reason) for category, reason in dismissals] == _HELD
