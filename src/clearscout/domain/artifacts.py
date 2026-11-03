"""The closed vocabulary of stored artifact kinds, and the raw posting source kept beside a body."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

# Store only the closed artifact vocabulary and the three user-file kinds.
ArtifactLogicalType = Literal[
    "posting.body.text",
    "posting.body.html",
    "posting.listing.text",
    "posting.source.json",
    "posting.source.html",
    "posting.source.xml",
    "posting.source.pdf",
    "posting.source.image",
    "master_cv.snapshot.markdown",
    "master_cv.snapshot.text",
    "evaluation.fit_report.markdown",
    "screening.report.markdown",
    "batch.report.markdown",
    "batch.record.json",
    "discovery.session.json",
    "resume.markdown",
    "cover_letter.markdown",
    "tailoring.report.markdown",
    "tailoring.manifest.json",
    "tailoring.review.json",
    "tailoring.review.markdown",
    "company_research.posting.text",
    "company_research.careers.text",
    "company_research.about.text",
    "document.rendered.pdf",
    "document.rendered.text",
    "user.attachment",
    "recruiter.email",
    "take_home",
]

ARTIFACT_LOGICAL_TYPES: tuple[str, ...] = get_args(ArtifactLogicalType)
ArtifactAuthorType = Literal["human", "model", "system"]
ARTIFACT_AUTHOR_TYPES: tuple[str, ...] = get_args(ArtifactAuthorType)
ArtifactIntegrityStatus = Literal[
    "unverified",
    "staged",
    "verified",
    "unresolved",
    "missing",
    "changed",
]
ARTIFACT_INTEGRITY_STATUSES: tuple[str, ...] = get_args(ArtifactIntegrityStatus)


@dataclass(frozen=True)
class PostingSource:
    """What one posting body was converted from, as the read received it."""

    # The board platform, or "page" for a fetched posting page.
    publisher: str
    # JSON text of the per-posting board record, or of the page's JobPosting node.
    record: str = ""
    description_markup: str = ""
    # Where the record states the markup: dotted key paths, or the derivation that chose it.
    description_paths: tuple[str, ...] = ()
    page_html: str = ""
    # A feed's own item markup as ElementTree re-serializes it.
    item_xml: str = ""
    # JSON of the application form a second read of the posting fetched, kept out
    # of ``record`` because the posting API did not send it.
    application_form: str = ""


ResearchKind = Literal["posting", "careers", "about"]

# Keep one artifact kind per research source.
RESEARCH_ARTIFACT_TYPES: dict[ResearchKind, ArtifactLogicalType] = {
    "posting": "company_research.posting.text",
    "careers": "company_research.careers.text",
    "about": "company_research.about.text",
}
