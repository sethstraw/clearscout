"""Rewrite version 3 and 4 staged-facts documents as version 5."""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAGED_FACTS_VERSION = 5
_EMPTY_FACT = {"values": [], "unresolved_values": [], "provenance": "", "complete": False}
# The keys a version 5 document holds in order, each with what the reader takes when it is missing.
STAGED_FACTS_DEFAULTS: dict[str, object] = {
    "schema_version": STAGED_FACTS_VERSION,
    "identity_trust": "unverified_hint",
    "employer": _EMPTY_FACT,
    "role_aliases": _EMPTY_FACT,
    "locations": _EMPTY_FACT,
    "arrangements": _EMPTY_FACT,
    "employment_types": _EMPTY_FACT,
    "compensation": _EMPTY_FACT,
    "freshness": "unknown",
    "focus_terms": _EMPTY_FACT,
    "closing_date": None,
    "posted_date": None,
}
_OLD_VERSIONS = (3, 4)


def upgraded_staged_facts(document: dict[str, object]) -> dict[str, object]:
    """Return an old document with every missing key at its empty value and version 5."""
    merged = {
        key: document.get(key, json.loads(json.dumps(value)))
        for key, value in STAGED_FACTS_DEFAULTS.items()
    }
    merged.update({key: value for key, value in document.items() if key not in merged})
    merged["schema_version"] = STAGED_FACTS_VERSION
    return merged


def upgrade() -> None:
    """Fill the keys version 5 added into every older staged-facts document."""
    bind = op.get_bind()
    table = sa.table("pending_discoveries", sa.column("id"), sa.column("staged_facts", sa.JSON))
    for row_id, stored in bind.execute(sa.select(table.c.id, table.c.staged_facts)):
        document = json.loads(stored) if isinstance(stored, str) else stored
        if not isinstance(document, dict) or document.get("schema_version") not in _OLD_VERSIONS:
            continue
        bind.execute(
            table.update()
            .where(table.c.id == row_id)
            .values(staged_facts=upgraded_staged_facts(document))
        )


def downgrade() -> None:
    """Version 5 documents stay; nothing reads the older versions."""
