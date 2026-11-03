"""Deterministic synthetic ``CvInventory`` for the harness.

The scripted solvers and, by default, the live solvers render this citable-entry
vocabulary, so a run does not depend on whichever workspace is open.

The employer name and the email domain come from the closed vocabularies the
public-data audit accepts under ``evals/``: ``EVAL_EMPLOYERS`` for employers
and ``SAFE_EMAIL_DOMAINS`` for email domains.
"""

from __future__ import annotations

from pathlib import Path

from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind, Role, content_hash
from clearscout.application.tailoring.guardrails import Guardrails

SYNTHETIC_SOURCE = Path("/evals/support/synthetic_cv.md")
SYNTHETIC_WORK_AUTHORIZATION = "Authorized to work in Canada without sponsorship."
SYNTHETIC_LOCATION = "Located in Calgary, Alberta, Canada."
SYNTHETIC_ORGANIZATION = "Northwind Systems"
SYNTHETIC_TITLE_OF_RECORD = "Staff Engineer - Payments Platform"
SYNTHETIC_ENTRY_TEXTS = (
    "Delivered a Python ingestion service that processed thirty million payment events per day.",
    "Replaced scheduled cron tasks with Kubernetes CronJobs for the reconciliation pipeline.",
    "Led the on-call rotation and coordinated incident response for the settlements service.",
    (
        "Worked well with design, customer support, and engineering partners "
        "to deliver three releases."
    ),
    (
        "Helped teammates resolve delivery blockers and kept shared commitments "
        "visible during weekly planning."
    ),
    (
        "Coordinated engineering and customer support partners during release planning, "
        "assigned owners, and delivered three releases."
    ),
    "Helped with production readiness and scaling work on the payments platform.",
)

SYNTHETIC_TEXT = (
    "# Alex Doe\n"
    "\n"
    f"{SYNTHETIC_WORK_AUTHORIZATION}\n"
    f"{SYNTHETIC_LOCATION}\n"
    "\n"
    "## Professional Experience\n"
    "\n"
    f"### {SYNTHETIC_TITLE_OF_RECORD}, {SYNTHETIC_ORGANIZATION}\n"
    "\n"
) + "".join(f"- {text}\n" for text in SYNTHETIC_ENTRY_TEXTS)


def synthetic_guardrails() -> Guardrails:
    """Return the deterministic asserted status the evaluation solvers share."""
    return Guardrails(work_authorization=SYNTHETIC_WORK_AUTHORIZATION)


def synthetic_inventory() -> CvInventory:
    """Return the deterministic inventory the scripted and live solvers share."""
    role = Role(
        key="northwind-staff-engineer",
        organization=SYNTHETIC_ORGANIZATION,
        title="Staff Engineer",
        title_of_record=SYNTHETIC_TITLE_OF_RECORD,
        dates="",
        current=True,
        order=1,
    )
    entries = tuple(
        Entry(
            id=f"syn-e{position}",
            kind=EntryKind.BULLET,
            section="Professional Experience",
            role_key=role.key,
            text=text,
            order=position,
        )
        for position, text in enumerate(SYNTHETIC_ENTRY_TEXTS, 1)
    )
    return CvInventory(
        source=SYNTHETIC_SOURCE,
        content_hash=content_hash(SYNTHETIC_TEXT),
        name="Alex Doe",
        contact_lines=(SYNTHETIC_LOCATION,),
        sections=("Professional Experience",),
        roles=(role,),
        entries=entries,
    )
