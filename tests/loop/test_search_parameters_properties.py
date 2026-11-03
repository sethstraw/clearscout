"""Property tests for search-parameter model roundtrip.

`JobSearchParameters` and its sub-models are Pydantic StrictModels with
after-validators. The property under test: a valid instance JSON-roundtrips
to an equal instance. This catches divergence between input coercion and
output serialization that an example test would miss on unusual field
combinations.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    JobSearchParameters,
    Location,
    Seniority,
    TargetTitle,
    WorkEligibility,
    WorkingTimeZone,
)

_ACCEPTANCE = st.one_of(st.none(), st.sampled_from(list(Acceptance)))
_KEY = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=20)
_COUNTRY = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=20)
_LINE = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=0, max_size=40)


@st.composite
def _arrangements(draw: st.DrawFn) -> Arrangement:
    return Arrangement(
        remote=draw(_ACCEPTANCE),
        hybrid=draw(_ACCEPTANCE),
        on_site=draw(_ACCEPTANCE),
        maximum_office_days=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=7))),
        maximum_travel_percent=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=100))),
        relocation=draw(st.one_of(st.none(), st.booleans())),
        onsite_requires_paid_travel=draw(st.one_of(st.none(), st.booleans())),
    )


@st.composite
def _locations(draw: st.DrawFn) -> list[Location]:
    """Return zero to three locations with distinct keys."""
    n = draw(st.integers(min_value=0, max_value=3))
    keys = draw(st.lists(_KEY, min_size=n, max_size=n, unique=True))
    return [
        Location(
            key=keys[index],
            country=draw(_COUNTRY),
            region=draw(_LINE),
            metro=draw(_LINE),
            eligibility=draw(st.sampled_from(list(WorkEligibility))),
            basis=draw(_LINE),
            aliases=draw(st.lists(_COUNTRY, min_size=0, max_size=3)),
            arrangement=draw(st.one_of(st.none(), _arrangements())),
        )
        for index in range(n)
    ]


@st.composite
def _seniorities(draw: st.DrawFn) -> Seniority:
    """Return a Seniority of excluded titles; bounds and target titles stay at their defaults."""
    return Seniority(
        excluded_titles=draw(st.lists(_LINE, min_size=0, max_size=3, unique=True)),
    )


@st.composite
def _job_search_parameters(draw: st.DrawFn) -> JobSearchParameters:
    return JobSearchParameters(
        locations=draw(_locations()),
        working_time_zone=draw(st.one_of(st.none(), st.sampled_from(list(WorkingTimeZone)))),
        working_day_shift_hours=draw(st.integers(min_value=0, max_value=12)),
        arrangement=draw(_arrangements()),
        seniority=draw(_seniorities()),
    )


@given(_arrangements())
def test_an_arrangement_json_roundtrips_to_itself(arrangement: Arrangement) -> None:
    """`model_validate_json(model_dump_json())` returns an equal instance."""
    restored = Arrangement.model_validate_json(arrangement.model_dump_json())
    assert restored == arrangement


@given(_job_search_parameters())
def test_search_parameters_json_roundtrips_to_themselves(
    parameters: JobSearchParameters,
) -> None:
    """A generated parameters object survives a JSON roundtrip unchanged."""
    restored = JobSearchParameters.model_validate_json(parameters.model_dump_json())
    assert restored == parameters


@given(_arrangements())
def test_layering_an_arrangement_over_the_zero_value_returns_itself(
    arrangement: Arrangement,
) -> None:
    """`Arrangement.over(Arrangement())` is the identity: the zero value carries nothing."""
    assert arrangement.over(Arrangement()) == arrangement


def test_a_bare_title_states_no_track() -> None:
    stated = Seniority.model_validate({"target_titles": ["Data Platform Lead"]})

    assert stated.stated_titles == ("Data Platform Lead",)
    assert stated.tracked_titles == (TargetTitle(title="Data Platform Lead"),)
    assert stated.target_titles == [TargetTitle(title="Data Platform Lead")]
