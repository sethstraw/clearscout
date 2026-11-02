"""Canned model responses for schema-bound and independent transport tests.

``script`` derives evidence identifiers from the request schema so policy tests
cannot cite values the request withheld. ``independent_script`` deliberately
does not: its fixed response is the full-pipeline oracle that catches a schema
and response changing together.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from tests.support.schema import declares, literals

Schema = type[BaseModel] | dict[str, Any]
Payload = Callable[[Any], dict[str, Any]]


def script(sources: dict[str, Any]) -> dict[str, Payload]:
    """Build the scripted responses for one run."""

    def jobspec(schema: Schema) -> dict[str, Any]:
        requirements = [
            {"text": "Kafka", "importance": "must_have"},
            {"text": "Schema governance", "importance": "must_have"},
            {"text": "Data lineage", "importance": "nice_to_have"},
            {"text": "Terraform", "importance": "nice_to_have"},
        ]
        sources["requirement_count"] = len(requirements)
        return {
            "title": "Staff Data Platform Engineer",
            "company": "Globex",
            "seniority": "staff",
            "location": "Remote, United States",
            "requirements": requirements,
            "keywords": ["Kafka", "Terraform", "schema registry", "data lineage", "PostgreSQL"],
            "hard_gates": [
                {"kind": "sponsorship", "text": "No visa sponsorship", "blocking": True}
            ],
        }

    def evidence(schema: Schema) -> dict[str, Any]:
        entry_ids = literals(schema, "entry_ids")
        sources["entry_ids"] = entry_ids
        # Cite experience bullets, the way a real match would. Citing whatever
        # comes first selects summary and skills entries, which produces a resume
        # with no experience on it.
        bullets = [entry_id for entry_id in entry_ids if entry_id.startswith("b-")] or entry_ids
        # Publications are cited too, because selection only includes them when
        # the posting gives a reason and a scripted matcher that never cites one
        # would leave every pipeline test with no publications to reason about.
        papers = [entry_id for entry_id in entry_ids if entry_id.startswith("pub-")]
        # The schema offers a fixed block of identifiers, so answer only the
        # requirements the task block listed, the way a real matcher does.
        offered = list(literals(schema, "requirement_id"))
        requirements = offered[: sources.get("requirement_count", len(offered))]
        matches = [
            {
                "requirement_id": requirement_id,
                "entry_ids": bullets[index * 2 : index * 2 + 3] or bullets[:3],
                "classification": "DIRECT",
                "strength": "strong",
                "note": "Direct experience.",
            }
            for index, requirement_id in enumerate(requirements)
        ]
        if papers and matches:
            matches[-1]["entry_ids"] = list(matches[-1]["entry_ids"]) + papers
        return {"matches": matches}

    def resume(schema: Schema) -> dict[str, Any]:
        entry_ids = literals(schema, "source_ids")
        sources["draft_ids"] = entry_ids
        first = entry_ids[0]
        bullets = [entry_id for entry_id in entry_ids if entry_id.startswith("b-")]
        skill_ids = [entry_id for entry_id in entry_ids if entry_id.startswith("skl-")]
        labelled = declares(schema, "label")
        items = (
            ("Platforms", "Kafka, Terraform, PostgreSQL, Docker, Go, Python"),
            ("Governance", "schema registry, data lineage, data quality rules, cataloging"),
        )
        skills = [
            {**({"label": label} if labelled else {}), "text": text, "source_ids": [cited]}
            for cited, (label, text) in zip(skill_ids, items, strict=False)
        ]
        return {
            "summary": {
                "text": (
                    "Jane Example builds streaming ingestion and schema registry systems. "
                    "Data lineage work uses Terraform and PostgreSQL."
                ),
                "source_ids": bullets[:3] or [first],
            },
            # A draft schema over a CV with no skill entries has no skills field at all.
            **({"skills": skills} if skills else {}),
            "roles": [
                {
                    "role_key": role_key,
                    "context": {
                        "text": "Platform architecture work alongside analytics and compliance.",
                        "source_ids": [first],
                    },
                    "bullets": [
                        {
                            "text": "Delivered platform work from cited evidence.",
                            "source_ids": [entry_id],
                        }
                        for entry_id in bullets
                        if _FIXED_BULLET_ROLES.get(entry_id, role_key) == role_key
                    ],
                }
                for role_key in literals(schema, "role_key")
            ],
            # A plan with no earlier role leaves the field out of the schema.
            **(
                {
                    "earlier_career": {
                        "text": (
                            "Earlier roles covered analytics engineering and reporting pipelines."
                        ),
                        "source_ids": [first],
                    }
                }
                if declares(schema, "earlier_career")
                else {}
            ),
        }

    def critic(schema: Schema) -> dict[str, Any]:
        return {
            "alignment": 5,
            "preservation": 5,
            "specificity": 5,
            "readability": 5,
            "ats_clarity": 5,
            "human_voice": 5,
            "unsupported_claims": [],
            "style_flags": [],
            "weaknesses": [],
            "guidance": "Reads well.",
        }

    def letter(schema: Schema) -> dict[str, Any]:
        first = literals(schema, "source_ids")[0]
        return {"paragraphs": [{"text": "The record fits this posting.", "source_ids": [first]}]}

    return {
        "JobSpecExtraction": jobspec,
        "EvidenceMap": evidence,
        "ResumeDraft": resume,
        "CriticVerdict": critic,
        "CoverLetter": letter,
    }


_FIXED_BULLET_IDS = (
    "b-a4aa0046",
    "b-b1ea77ab",
    "b-fb17c65d",
    "b-7e6b5bbe",
    "b-7d13a376",
    "b-b0c1083f",
    "b-59bfac38",
    "b-c7fe1af5",
)
_FIXED_BULLET_ROLES = {
    entry_id: role
    for role, ids in (
        ("northwind-analytics", _FIXED_BULLET_IDS[:5]),
        ("contoso-health", _FIXED_BULLET_IDS[5:7]),
        ("initech-systems", _FIXED_BULLET_IDS[7:]),
    )
    for entry_id in ids
}

_FIXED_ROLE_KEYS = ("northwind-analytics", "contoso-health", "initech-systems")
_FIXED_SKILL_ID = "skl-b6731d7d"


def independent_script() -> dict[str, Payload]:
    """Return responses fixed independently of the request schemas.

    The identifiers are taken from the checked-in CV fixture once, so this
    transport test fails if the fixture changes instead of adapting its answer.
    """

    def jobspec(_: Schema) -> dict[str, Any]:
        return {
            "title": "Staff Data Platform Engineer",
            "company": "Globex",
            "seniority": "staff",
            "location": "Remote, United States",
            "requirements": [
                {"text": "Kafka", "importance": "must_have"},
                {"text": "Schema governance", "importance": "must_have"},
                {"text": "Data lineage", "importance": "nice_to_have"},
                {"text": "Terraform", "importance": "nice_to_have"},
            ],
            "keywords": ["Kafka", "Terraform", "schema registry", "data lineage"],
            "hard_gates": [
                {"kind": "sponsorship", "text": "No visa sponsorship", "blocking": True}
            ],
        }

    def evidence(_: Schema) -> dict[str, Any]:
        return {
            "matches": [
                {
                    "requirement_id": f"r{index:02d}",
                    "entry_ids": list(_FIXED_BULLET_IDS[index * 2 - 2 : index * 2]),
                    "classification": "DIRECT",
                    "strength": "strong",
                    "note": "Direct experience.",
                }
                for index in range(1, 5)
            ]
        }

    def resume(_: Schema) -> dict[str, Any]:
        first = _FIXED_BULLET_IDS[0]
        return {
            "summary": {
                "text": "A platform engineer focused on streaming ingestion and schema governance.",
                "source_ids": [first],
            },
            "skills": [
                {
                    "text": "Kafka, Terraform, PostgreSQL",
                    "source_ids": [_FIXED_SKILL_ID],
                }
            ],
            "roles": [
                {
                    "role_key": role_key,
                    "context": {"text": "Led platform architecture.", "source_ids": [first]},
                    "bullets": [
                        {
                            "text": "Delivered platform work from cited evidence.",
                            "source_ids": [entry_id],
                        }
                        for entry_id in _FIXED_BULLET_IDS
                        if _FIXED_BULLET_ROLES[entry_id] == role_key
                    ],
                }
                for role_key in _FIXED_ROLE_KEYS
            ],
        }

    def critic(_: Schema) -> dict[str, Any]:
        return {
            "alignment": 5,
            "preservation": 5,
            "specificity": 5,
            "readability": 5,
            "ats_clarity": 5,
            "human_voice": 5,
            "unsupported_claims": [],
            "style_flags": [],
            "weaknesses": [],
            "guidance": "Reads well.",
        }

    def letter(_: Schema) -> dict[str, Any]:
        return {
            "paragraphs": [
                {"text": "The record fits this posting.", "source_ids": [_FIXED_BULLET_IDS[0]]}
            ]
        }

    return {
        "JobSpecExtraction": jobspec,
        "EvidenceMap": evidence,
        "ResumeDraft": resume,
        "CriticVerdict": critic,
        "CoverLetter": letter,
    }
