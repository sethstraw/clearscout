"""What a verified publisher body refuses, and the pay it states."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from tailorcv.application.contracts import CompensationInput
from tailorcv.application.discovery.dispositions import (
    BODY_REFUSAL_CATEGORIES,
    GATE_REFUSAL_CATEGORIES,
)
from tailorcv.application.discovery.posting_text import posting_facts, role_based_place
from tailorcv.application.discovery.screening import (
    GENERIC_US_REMOTE,
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
    office_only_at_remote_only_sites,
    screen_discovery,
    stated_places,
)
from tailorcv.application.tailoring import fit
from tailorcv.application.workspaces.parameters import SearchParametersService
from tailorcv.domain.arrangement_grammar import remote_perk_or_culture
from tailorcv.domain.artifacts import PostingSource
from tailorcv.domain.identity import mentions, normalize_text
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.domain.search_parameters.conditions import excluded_by
from tailorcv.domain.staged_facts import PublishedFact, StagedListingFacts
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PendingDiscovery
from tailorcv.infrastructure.web_fetch.application_form import required_residence_question
from tailorcv.infrastructure.web_fetch.ingest import MINIMUM_USEFUL_CHARACTERS
from tailorcv.infrastructure.web_fetch.listing_facts import REMOTE_PLACE
from tailorcv.shared.text import sanitize

_BODY_REFUSAL_CODES = frozenset(ScreeningCode(category) for category in BODY_REFUSAL_CATEGORIES)


_BODY_REFUSAL_DIMENSIONS = {
    ScreeningCode.ARRANGEMENT_REFUSED: ScreeningDimension.ARRANGEMENT,
    ScreeningCode.ARRANGEMENT_OFFICE_LIMIT: ScreeningDimension.ARRANGEMENT,
    ScreeningCode.TRAVEL_OVER_LIMIT: ScreeningDimension.ARRANGEMENT,
    ScreeningCode.CONDITION_EXCLUDED: ScreeningDimension.CONDITION,
    ScreeningCode.SENIORITY_OUTSIDE: ScreeningDimension.SENIORITY,
}


_PUBLISHER_LOCATION = re.compile(r"(?im)^\s*(?:[-*]\s*)?location\s*:\s*(?P<value>[^\n]{1,500})$")


def _body_location(text: str) -> str:
    """Return the publisher body's stated location, dropping a generic US-remote phrase."""
    location_match = _PUBLISHER_LOCATION.search(text)
    location = location_match.group("value").strip() if location_match else ""
    return "" if GENERIC_US_REMOTE.fullmatch(sanitize(location)) else location


def _body_location_fact(location: str, staged: StagedListingFacts) -> PublishedFact:
    """Prefer a fresh body location read over the staged one, when the body names one.

    A staged "N Locations" summary the body read does not repeat stays
    incomplete: one body line is not proof the other stated locations agree.
    """
    if not location:
        return staged.locations
    stated = staged.locations.values
    # One body line replaces a multi-place list only when it names every staged place.
    covered = len(stated) <= 1 or all(value.casefold() in location.casefold() for value in stated)
    return PublishedFact(
        values=(location,),
        unresolved_values=staged.locations.unresolved_values,
        provenance="publisher_body.location",
        complete=staged.locations.complete and covered,
    )


_BODY_PAY_PROVENANCE = "publisher_body.compensation"


_PAY_PERIODS = {"annual": "year", "monthly": "month", "daily": "day", "hourly": "hour"}


def _stated_pay(pay: CompensationInput) -> str:
    """Write one extracted range in the form screening reads a listing's pay."""
    low, high = pay.amount_min, pay.amount_max
    span = f"{low:f}" if high is None or high == low else f"{low:f}-{high:f}"
    return f"{pay.currency} {span} per {_PAY_PERIODS[pay.basis]}"


def body_pay(reading: fit.Reading) -> tuple[str, ...]:
    """Return the pay a body's fit reading quotes, written as a listing states pay."""
    return tuple(_stated_pay(pay) for pay in reading.pay_extractions if pay.amount_min is not None)


def with_body_pay(staged: StagedListingFacts, pay: tuple[str, ...]) -> StagedListingFacts:
    """Return staged facts carrying a body's pay, unless the listing states pay.

    Body pay stays incomplete, so it shows on the row but never refuses one.
    """
    held = staged.compensation
    owned = held.provenance == _BODY_PAY_PROVENANCE
    if not owned and (held.values or held.unresolved_values):
        return staged
    if pay:
        return replace(
            staged, compensation=PublishedFact(values=pay, provenance=_BODY_PAY_PROVENANCE)
        )
    return replace(staged, compensation=PublishedFact()) if owned else staged


def take_body_pay(pending: PendingDiscovery, reading: fit.Reading | None) -> None:
    """Save the pay a verified body's reading quotes onto the row."""
    if reading is None:
        return
    staged = StagedListingFacts.from_json(pending.staged_facts)
    pending.staged_facts = with_body_pay(staged, body_pay(reading)).as_json()


@dataclass(frozen=True)
class BodyScreen:
    """A verified body's stated conflict, if any, and the fit reading its pay is taken from."""

    refusal: tuple[ScreeningCode, str] | None
    # None when the attempt kept no verified, active body to read.
    pay_reading: fit.Reading | None
    # The body-refusal categories this screen shows the posting no longer conflicts with.
    cleared: frozenset[str] = frozenset()


def _cleared_categories(screening: DiscoveryScreening, *, body_placed: bool) -> frozenset[str]:
    """Return the body-refusal categories a refusal-free screen positively clears."""
    # A place the row's facts gave and the body never names may only have been read
    # differently, so a location refusal clears on a match or a place the body states.
    placed = body_placed or any(
        item.dimension is ScreeningDimension.LOCATION and item.standing is ScreeningStanding.MATCH
        for item in screening.findings
    )
    return BODY_REFUSAL_CATEGORIES - (set() if placed else {ScreeningCode.LOCATION_OUTSIDE.value})


def _arrangement_open(staged: StagedListingFacts, places: PublishedFact) -> bool:
    """Return whether a body read may not narrow the row's arrangement.

    A row staged or listed with a remote place, or unresolved, stays open, and so does
    one whose places are incomplete, since attendance refuses only against every place
    the posting names.
    """
    listed = (*staged.arrangements.values, *staged.locations.values, *places.values)
    return bool(
        staged.arrangements.unresolved_values
        or any(REMOTE_PLACE.search(value) for value in listed)
        or not places.complete
        or places.unresolved_values
    )


def _body_refusal_finding(
    screening: DiscoveryScreening, codes: frozenset[ScreeningCode] = _BODY_REFUSAL_CODES
) -> ScreeningFinding | None:
    return next(
        (
            item
            for item in screening.findings
            if item.standing is ScreeningStanding.REFUSE and item.code in codes
        ),
        None,
    )


def _vouched_title(pending: PendingDiscovery, staged: StagedListingFacts, text: str) -> str:
    """Return the title the row or its page vouches for, or nothing when only a hint names it."""
    if staged.publisher_vouched:
        return pending.title
    if pending.title.strip() and mentions(normalize_text(text), normalize_text(pending.title)):
        return pending.title
    return posting_facts(text).title


def _quoted_subject(finding: ScreeningFinding, title: str, location: str) -> str:
    """Return a finding's subject beside the title or place text it was read from."""
    quoted = title if finding.code is ScreeningCode.SENIORITY_OUTSIDE else location
    return f"{finding.subject}: {quoted}" if quoted.strip() else finding.subject


_FIT_GATE_CODES = {gate: ScreeningCode(code) for gate, code in GATE_REFUSAL_CATEGORIES.items()}


_ATTENDANCE_GATES = frozenset({"arrangement", "office attendance"})


def _fit_refusal(
    text: str,
    parameters: JobSearchParameters,
    place: str | None,
    listing_location: str,
    *,
    arrangement_open: bool,
) -> BodyScreen:
    """Return the first residence, arrangement, travel, or credential gate a body's fit fails.

    Attendance and travel are read against the listing's own place.
    """
    placed = parameters.model_copy(update={"arrangement": parameters.arrangement_for(place)})
    # Every place without its own block inherits the general relocation answer.
    reading = replace(
        fit.read(text, parameters=placed),
        residence_requirement=fit.residence_requirement(text, parameters, listing_location),
    )
    found = tuple(
        gate
        for gate in fit.gates(reading, placed)
        if gate.name in _FIT_GATE_CODES
        and not (arrangement_open and gate.name in _ATTENDANCE_GATES)
    )
    failed = fit.refusal(found)
    if failed is None:
        return BodyScreen(None, reading)
    evidence = dict.fromkeys((failed.reason, *_gate_evidence(failed.name, reading, placed)))
    return BodyScreen((_FIT_GATE_CODES[failed.name], "; ".join(evidence)), reading)


def _gate_evidence(
    name: str, reading: fit.Reading, parameters: JobSearchParameters
) -> tuple[str, ...]:
    """Return the posting text a failed gate read."""
    if name == "travel":
        return reading.travel_fit.evidence
    if name == "residence requirement":
        return reading.residence_requirement.evidence
    # The arrangement gate's reason already quotes its clauses.
    if name == "arrangement":
        return ()
    if name == "office attendance":
        return reading.arrangement_fit.evidence
    return tuple(
        line.whole
        for line in reading.requirements
        if line.stance is fit.Stance.MUST_HAVE
        and excluded_by(parameters.excluded_conditions, (line.whole,)) is not None
    )[:1]


def _presumed_on_site(
    facts: StagedListingFacts,
    location: str,
    text: str,
    screen: BodyScreen,
    parameters: JobSearchParameters,
    title: str,
) -> tuple[ScreeningCode, str] | None:
    """Refuse a named site at remote-only places when no listing or body states an arrangement."""
    arrangements = facts.arrangements
    reading = screen.pay_reading.location if screen.pay_reading is not None else None
    # The fit reader also takes a perk or culture line as remote; that alone states nothing.
    stated = reading is not None and (
        reading.value not in (None, "remote")
        or not all(remote_perk_or_culture(line) for line in reading.evidence)
    )
    if stated or arrangements.values or arrangements.unresolved_values:
        return None
    if not office_only_at_remote_only_sites(facts, location, parameters, text, title=title):
        return None
    return ScreeningCode.ARRANGEMENT_REFUSED, f"no working arrangement stated: {location}"


_FORM_RESIDENCE_SUBJECT = "the application form requires a US state of residence: "


def _form_residence_refusal(
    source: PostingSource | None, parameters: JobSearchParameters
) -> tuple[ScreeningCode, str] | None:
    """Return the refusal a form question accepting only a US state of residence states."""
    label = None if source is None else required_residence_question(source)
    return None if label is None else _form_residence_answer(label, parameters)


def _form_residence_answer(
    label: str, parameters: JobSearchParameters
) -> tuple[ScreeningCode, str] | None:
    """Refuse, as a body's residence rule does, only a marked home outside the United States."""
    lives = fit.resident_countries(parameters)
    if not lives or "united states" in lives:
        return None
    return ScreeningCode.LOCATION_RESIDENCE_REQUIRED, f"{_FORM_RESIDENCE_SUBJECT}{label}"


def _judged_form_question(screening: Mapping[str, Any]) -> str | None:
    """Return the form question a body-screen dismissal's snapshot refused on, if any."""
    findings = screening.get("findings")
    for finding in findings if isinstance(findings, list) else ():
        subject = finding.get("subject") if isinstance(finding, Mapping) else None
        if isinstance(subject, str) and subject.startswith(_FORM_RESIDENCE_SUBJECT):
            return subject.removeprefix(_FORM_RESIDENCE_SUBJECT)
    return None


def rescreened_body_refusal(
    pending: PendingDiscovery,
    body: bytes,
    workspace_id: str,
    database: Database,
    judged: Mapping[str, Any],
) -> tuple[ScreeningCode, str] | None:
    """Return the refusal a retained body still states under the current search rules.

    The application form is not retained beside the body, so a form question the
    judged dismissal recorded is read again from that record. A judged location
    refusal stands until the place reads inside or the body states one.
    """
    screen = body_screen(pending, body, workspace_id, database)
    if screen.refusal is not None:
        return screen.refusal
    judged_place = next(
        (
            str(item.get("subject", ""))
            for item in judged.get("findings", [])
            if item.get("code") == ScreeningCode.LOCATION_OUTSIDE.value
        ),
        None,
    )
    if judged_place is not None and ScreeningCode.LOCATION_OUTSIDE.value not in screen.cleared:
        return ScreeningCode.LOCATION_OUTSIDE, judged_place
    label = _judged_form_question(judged)
    if label is None:
        return None
    return _form_residence_answer(label, SearchParametersService(database).current(workspace_id))


def body_screen(
    pending: PendingDiscovery,
    body: bytes,
    workspace_id: str,
    database: Database,
    source: PostingSource | None = None,
) -> BodyScreen:
    """Return only a definite conflict explicitly stated by verified body text, and its reading."""
    text = body.decode("utf-8", errors="replace")
    location = _body_location(text)
    staged = StagedListingFacts.from_json(pending.staged_facts)
    # Only a row nothing else places reads a role sentence; the places fact stays incomplete.
    if not (
        _PUBLISHER_LOCATION.search(text) or pending.location.strip() or staged.locations.values
    ):
        location = role_based_place(text)
    parameters = SearchParametersService(database).current(workspace_id)
    searched = list(parameters.seniority.stated_titles)
    title = _vouched_title(pending, staged, text)
    # Staging already read a vouched title's ladder; only a page vouching a hint adds one.
    codes = (
        _BODY_REFUSAL_CODES - {ScreeningCode.SENIORITY_OUTSIDE}
        if staged.publisher_vouched
        else _BODY_REFUSAL_CODES
    )
    places = _body_location_fact(location, staged)
    screening = screen_discovery(
        pending.employer,
        title,
        location,
        parameters,
        searched,
        StagedListingFacts(locations=places),
    )
    finding = _body_refusal_finding(screening, codes)
    if finding is not None:
        return BodyScreen(
            (finding.code, _quoted_subject(finding, title, location or pending.location)),
            fit.read(text, parameters=parameters, listing_location=pending.location),
        )
    place = next(
        (item.subject for item in screening.findings if item.code is ScreeningCode.LOCATION_MATCH),
        None,
    )
    screen = _fit_refusal(
        text,
        parameters,
        place,
        pending.location,
        arrangement_open=_arrangement_open(staged, places),
    )
    if screen.refusal is None:
        screen = replace(screen, refusal=_form_residence_refusal(source, parameters))
    if screen.refusal is None:
        located = replace(staged, locations=places)
        place_text = location or pending.location
        screen = replace(
            screen, refusal=_presumed_on_site(located, place_text, text, screen, parameters, title)
        )
    if screen.refusal is None:
        screen = replace(
            screen, cleared=_cleared_categories(screening, body_placed=bool(location.strip()))
        )
    # The gate reading places a bare dollar without the listing, so a listed row reads pay again.
    if not pending.location:
        return screen
    pay_reading = fit.read(text, parameters=parameters, listing_location=pending.location)
    return replace(screen, pay_reading=pay_reading)


def useful_body(content: bytes) -> bool:
    """Return whether retained bytes hold enough text to stand as a posting body."""
    return len(content.decode("utf-8", errors="ignore").strip()) >= MINIMUM_USEFUL_CHARACTERS


def row_places(pending: PendingDiscovery) -> tuple[str, ...]:
    """Return the places this row states, from its staged locations and its location text."""
    return stated_places(
        StagedListingFacts.from_json(pending.staged_facts).locations, pending.location
    )


def family_order(
    row: PendingDiscovery,
    parameters: JobSearchParameters,
    searched: list[str],
    *,
    body_refused: bool = False,
) -> tuple[int, date, str]:
    """Order one regional family, a copy whose place and arrangement the person accepts first.

    A candidate whose body stated a refusal was dismissed by that read, so only the
    row being read now carries one its staged facts do not state.
    """
    screening = screen_discovery(
        row.employer,
        row.title,
        row.location,
        parameters,
        searched,
        StagedListingFacts.from_json(row.staged_facts),
    )
    refused = body_refused or _body_refusal_finding(screening) is not None
    # Identifiers are random, so two copies first seen on one day are ordered by URL.
    return (1 if refused else 0, row.first_seen_on, row.discovery_url)


def body_screening_snapshot(
    code: ScreeningCode, subject: str, content_sha256: str | None
) -> dict[str, Any]:
    """Preserve the body-backed hard finding and the judged body in the reversible event."""
    dimension = _BODY_REFUSAL_DIMENSIONS.get(code, ScreeningDimension.LOCATION)
    return {
        "content_sha256": content_sha256,
        "standing": ScreeningStanding.REFUSE.value,
        "findings": [
            {
                "dimension": dimension.value,
                "standing": ScreeningStanding.REFUSE.value,
                "code": code.value,
                "subject": subject,
            }
        ],
        "location_fact_complete": True,
    }
