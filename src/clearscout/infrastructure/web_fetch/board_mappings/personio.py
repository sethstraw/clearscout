"""Personio's posting record, as its per-tenant public XML feed states it."""

from __future__ import annotations

from xml.etree.ElementTree import Element

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_period,
    pay_range,
    stated_text,
)


def _url(posting: Posting) -> str:
    tenant = posting.groups.get("tenant", "")
    tld = posting.groups.get("tld") or "com"
    job_id = stated_text(posting.record, ("id",))
    # A position without an id has no posting address, so it is not a listing.
    return f"https://{tenant}.jobs.personio.{tld}/job/{job_id}?language=en" if job_id else ""


def _employer(posting: Posting) -> str:
    """A subcompany names the actual hiring entity before the configured employer."""
    return stated_text(posting.record, ("subcompany",)) or posting.employer


def _description(posting: Posting) -> str:
    """Join every <jobDescriptions><jobDescription> section's value, in feed order."""
    record = posting.record
    if not isinstance(record, Element):
        return ""
    sections = [
        stated_text(section, ("value",))
        for section in record.findall("./jobDescriptions/jobDescription")
    ]
    return "\n\n".join(section for section in sections if section)


def _compensation(posting: Posting) -> str | None:
    """Return the <salaryInformation> range; a key path stops at an element's text, so find it."""
    record = posting.record
    salary = record.find("salaryInformation") if isinstance(record, Element) else None
    if salary is None:
        return None
    return pay_range(
        stated_text(salary, ("currencyCode",)),
        stated_text(salary, ("min",)),
        stated_text(salary, ("max",)),
        pay_period(stated_text(salary, ("type",))),
    )


PERSONIO_MAPPING = BoardDeclaration(
    platform="personio",
    title=("name",),
    url=_url,
    requisition_id=("id",),
    employer=_employer,
    places=("office",),
    description=_description,
    compensation=_compensation,
)
