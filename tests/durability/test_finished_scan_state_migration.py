import json
import sqlite3
from pathlib import Path
from typing import Any

from clearscout.infrastructure.persistence.migration import downgrade, upgrade
from tests.support.database import insert_row


def _seed(path: Path) -> None:
    connection = sqlite3.connect(path)
    with connection:
        insert_row(connection, "workspaces", id="w")
        for source in ("s1", "s2"):
            insert_row(
                connection,
                "sources",
                id=source,
                workspace_id="w",
                source_type="employer_board",
                name=source,
            )
        for status in ("completed", "failed", "cancelled", "running"):
            insert_row(
                connection,
                "background_tasks",
                id=status,
                workspace_id="w",
                status=status,
                task_type="source.scan",
                idempotency_key=status,
            )
            insert_row(
                connection,
                "source_scan_members",
                id=f"m-{status}",
                workspace_id="w",
                root_task_id=status,
                source_id=f"src-{status}",
                state="completed",
                result=json.dumps({"board_result": {"rows": 3}, "staged": 2}),
            )
        scans = (
            ("old", "s1", "2026-01-01 08:00:00", "q-old"),
            ("new", "s1", "2026-01-02 08:00:00", "q-new"),
            ("only", "s2", "2026-01-01 08:00:00", "q-only"),
        )
        for scan, source, started, label in scans:
            insert_row(
                connection,
                "source_scans",
                id=scan,
                source_id=source,
                query_label=label,
                scan_started_at=started,
                exhausted_outcome="not_recorded",
            )
            insert_row(
                connection,
                "source_scan_continuations",
                id=f"c-{scan}",
                workspace_id="w",
                source_scan_id=scan,
                generation=1,
                handoff_scopes="[]",
            )
        for sha in ("sha-old", "sha-new", "sha-only", "sha-orphan"):
            insert_row(connection, "source_listing_memberships", sha256=sha, membership="[]")
        for scan in ("old", "new", "only"):
            insert_row(
                connection,
                "source_scan_frontier_items",
                id=f"f-{scan}",
                continuation_id=f"c-{scan}",
                normalized_url=f"https://jobs.example.test/{scan}",
                status="pending",
                depth=1,
                detail_checkpoint=json.dumps({"page": 2}),
                listing_membership_sha256=f"sha-{scan}",
            )
    connection.close()


def _state(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        return {
            "results": dict(connection.execute("SELECT id, result FROM source_scan_members")),
            "frontier": {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT id, detail_checkpoint, listing_membership_sha256 "
                    "FROM source_scan_frontier_items"
                )
            },
            "memberships": sorted(
                row[0]
                for row in connection.execute("SELECT sha256 FROM source_listing_memberships")
            ),
        }
    finally:
        connection.close()


def test_upgrade_releases_finished_and_superseded_scan_state(tmp_path: Path) -> None:
    path = tmp_path / "clearscout.sqlite3"
    url = f"sqlite+pysqlite:///{path}"
    upgrade(url, "0001")
    _seed(path)

    upgrade(url, "0002")
    state = _state(path)

    for status in ("completed", "failed", "cancelled"):
        assert json.loads(state["results"][f"m-{status}"]) == {"staged": 2}
    assert json.loads(state["results"]["m-running"]) == {"board_result": {"rows": 3}, "staged": 2}
    checkpoint = json.dumps({"page": 2})
    assert state["frontier"] == {
        "f-old": (None, None),
        "f-new": (checkpoint, "sha-new"),
        "f-only": (checkpoint, "sha-only"),
    }
    assert state["memberships"] == ["sha-new", "sha-only"]

    downgrade(url, "0001")
    upgrade(url, "0002")
    assert _state(path) == state
