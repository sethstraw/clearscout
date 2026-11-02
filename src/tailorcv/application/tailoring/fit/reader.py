"""The deterministic fit reading: one posting and one master CV into a Reading."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.fit.facts.arrangement import read_location
from tailorcv.application.tailoring.fit.facts.arrangement_lines import work_arrangement_lines
from tailorcv.application.tailoring.fit.facts.education import read_education, read_years_required
from tailorcv.application.tailoring.fit.facts.residence import (
    read_authorization,
    read_residence_requirement,
)
from tailorcv.application.tailoring.fit.facts.roles import (
    read_family,
    read_orientation,
    read_record_families,
    read_record_years,
    read_specialty,
)
from tailorcv.application.tailoring.fit.facts.seniority import read_seniority
from tailorcv.application.tailoring.fit.lexicon import REMOTE, refuses_remote
from tailorcv.application.tailoring.fit.preferences import (
    arrangement_fit,
    level_fit,
    pay_extractions,
    pay_fit,
    place_fit,
    time_zone_fit,
    travel_fit,
)
from tailorcv.application.tailoring.fit.structure import (
    Answer,
    Reading,
    Stance,
    content_lines,
    counted_at_stance,
    first_saying,
    posting_blocks,
    posting_title,
    requirement_lines,
    requirement_sections,
)
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.domain.taxonomy import Vocabulary, extended
from tailorcv.shared.text import sanitize


def read(
    posting: str,
    inventory: CvInventory | None = None,
    *,
    vocabulary: Vocabulary | None = None,
    parameters: JobSearchParameters | None = None,
    today: date | None = None,
    listing_location: str = "",
) -> Reading:
    """Read a posting and a master CV by rule, abstaining wherever the text is thin.

    Costs nothing and reaches nothing. The posting is the raw ingested text
    rather than the model's extraction of it, because half of what is derived
    here lives in the shape of the document. ``parameters`` adds the four
    questions that are about a person rather than about a posting, and they never
    change what the posting is recorded as saying. With no ``inventory`` the
    record answers abstain. ``listing_location`` places a posting whose body
    and title name none of the declared places.
    """
    if inventory is None:
        inventory = CvInventory(source=Path(), content_hash="", name="")
    lexicon = extended(vocabulary)
    wanted = parameters or JobSearchParameters()
    posting = sanitize(posting)
    blocks = posting_blocks(posting)
    title = posting_title(posting)
    body = "\n".join(content_lines(blocks))
    requirements = requirement_lines(blocks)
    family, family_answer = read_family(lexicon, title, body)
    years = read_years_required(requirements, blocks)
    arrangement_lines = work_arrangement_lines(blocks)
    location = read_location(arrangement_lines, title)
    seniority = read_seniority(title, body, years)
    # The listing's word counts only when the body states no arrangement of its own.
    remote = location.value == "remote" or (
        location.value is None
        and first_saying((listing_location,), REMOTE) is not None
        and not refuses_remote(listing_location)
    )
    place, place_key = place_fit(posting, blocks, wanted, listing_location, remote=remote)
    pay, pay_standing, pay_target_met, pay_above_stretch = pay_fit(
        blocks, wanted, place_key, listing=listing_location
    )
    extractions = pay_extractions(
        blocks, wanted, place_key, location.value or "", listing=listing_location
    )
    return Reading(
        role_family=family_answer,
        specialty=read_specialty(lexicon, family, body),
        orientation=read_orientation(blocks, title),
        seniority=seniority,
        years=years,
        education=read_education(requirements, blocks),
        location=location,
        authorization=read_authorization(blocks),
        sections=requirement_sections(blocks),
        must_have=counted_at_stance(requirements, Stance.MUST_HAVE),
        preferred=counted_at_stance(requirements, Stance.PREFERRED),
        record_families=read_record_families(lexicon, inventory),
        record_years=read_record_years(inventory, today or date.today()),
        place_fit=place,
        arrangement_fit=arrangement_fit(
            location, posting, blocks, wanted, place_key, arrangement_lines=arrangement_lines
        ),
        travel_fit=travel_fit(blocks, wanted, place_key),
        time_zone_fit=time_zone_fit(blocks, wanted),
        level_fit=level_fit(seniority, wanted),
        residence_requirement=read_residence_requirement(blocks, wanted, listing_location),
        pay_fit=pay,
        pay_standing=pay_standing,
        pay_target_met=pay_target_met,
        pay_above_stretch=pay_above_stretch,
        pay_extractions=extractions,
        requirements=requirements,
    )


def residence_requirement(
    posting: str, parameters: JobSearchParameters, listing_location: str = ""
) -> Answer[str]:
    """Read only the residence answer ``read`` gives for this posting and listing."""
    return read_residence_requirement(
        posting_blocks(sanitize(posting)), parameters, listing_location
    )
