import json
import sqlite3
from pathlib import Path
from typing import Any

from clearscout.domain.staged_facts import ListingIdentityTrust, PublishedFact, StagedListingFacts
from clearscout.infrastructure.persistence.migration import upgrade
from clearscout.infrastructure.persistence.migrations.versions.v0003_staged_facts_v5 import (
    STAGED_FACTS_DEFAULTS,
)
from tests.support.database import insert_row

_LOCATIONS = PublishedFact(values=("Freedonia",), provenance="fictional.location", complete=True)
_FOCUS = PublishedFact(values=("assay automation",), provenance="posting.summary", complete=True)
_EMPLOYER = PublishedFact(values=("Acme Bio",), provenance="fictional.employer", complete=True)


def _v5() -> dict[str, object]:
    return StagedListingFacts(
        locations=_LOCATIONS, focus_terms=_FOCUS, employer=_EMPLOYER
    ).as_json()


def _v3() -> dict[str, object]:
    keep = ["identity_trust", "role_aliases", "locations", "arrangements", "employment_types"]
    keep += ["compensation", "freshness"]
    document: dict[str, object] = {key: _v5()[key] for key in keep}
    return {"schema_version": 3, **document}


def _v4() -> dict[str, object]:
    document = {**_v3(), "schema_version": 4, "focus_terms": _FOCUS.as_json()}
    return {**document, "employer": _EMPLOYER.as_json(), "closing_date": None}


def _seed(path: Path, documents: dict[str, str]) -> None:
    connection = sqlite3.connect(path)
    with connection:
        insert_row(connection, "workspaces", id="w")
        for row_id, document in documents.items():
            insert_row(
                connection,
                "pending_discoveries",
                id=row_id,
                workspace_id="w",
                discovery_url=f"https://jobs.example.test/{row_id}",
                fingerprint=row_id,
                staged_facts=document,
            )
    connection.close()


def _stored(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        return dict(connection.execute("SELECT id, staged_facts FROM pending_discoveries"))
    finally:
        connection.close()


def test_defaults_equal_what_the_reader_takes_for_each_missing_key() -> None:
    missing_identity = StagedListingFacts(identity_trust=ListingIdentityTrust.UNVERIFIED_HINT)
    assert missing_identity.as_json() == STAGED_FACTS_DEFAULTS


def test_upgrade_rewrites_older_documents_as_the_version_a_fresh_stage_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "clearscout.sqlite3"
    url = f"sqlite+pysqlite:///{path}"
    upgrade(url, "0002")
    current = json.dumps(_v5(), separators=(",", ":"))
    no_identity = {key: value for key, value in _v4().items() if key != "identity_trust"}
    _seed(
        path,
        {
            "v3": json.dumps(_v3()),
            "v4": json.dumps(_v4()),
            "no-identity": json.dumps(no_identity),
            "v5": current,
        },
    )

    upgrade(url, "0003")
    stored = _stored(path)

    expected_v3 = StagedListingFacts(locations=_LOCATIONS).as_json()
    assert json.loads(stored["v3"]) == expected_v3
    assert json.loads(stored["v4"]) == _v5()
    assert stored["v5"] == current
    as_read = StagedListingFacts.from_json({**no_identity, "schema_version": 5})
    assert json.loads(stored["no-identity"]) == as_read.as_json()
