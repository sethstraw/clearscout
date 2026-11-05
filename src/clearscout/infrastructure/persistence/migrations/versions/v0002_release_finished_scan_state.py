"""Release board results of finished scans and checkpoints of superseded scans."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The status list is the terminal members of clearscout.domain.records.TaskStatus.
_SQLITE_FINISHED_RESULTS = """
UPDATE source_scan_members SET result = json_remove(result, '$.board_result')
WHERE json_type(result, '$.board_result') IS NOT NULL
  AND root_task_id IN (
    SELECT id FROM background_tasks WHERE status IN ('completed', 'failed', 'cancelled')
  )
"""
_POSTGRES_FINISHED_RESULTS = """
UPDATE source_scan_members SET result = (result::jsonb - 'board_result')::json
WHERE result::jsonb ? 'board_result'
  AND root_task_id IN (
    SELECT id FROM background_tasks WHERE status IN ('completed', 'failed', 'cancelled')
  )
"""

# A scan is superseded when its source has a scan ordered after it, newest first by
# scan_started_at, then created_at, as the scan-opening carry reads its prior scan.
_SUPERSEDED_CHECKPOINTS = """
UPDATE source_scan_frontier_items
SET detail_checkpoint = NULL, listing_membership_sha256 = NULL
WHERE (detail_checkpoint IS NOT NULL OR listing_membership_sha256 IS NOT NULL)
  AND continuation_id IN (
    SELECT c.id FROM source_scan_continuations c
    JOIN source_scans s ON s.id = c.source_scan_id
    WHERE EXISTS (
      SELECT 1 FROM source_scans n
      WHERE n.source_id = s.source_id
        AND (n.scan_started_at, n.created_at, n.id) > (s.scan_started_at, s.created_at, s.id)
    )
  )
"""

_UNNAMED_MEMBERSHIPS = """
DELETE FROM source_listing_memberships
WHERE NOT EXISTS (
  SELECT 1 FROM source_scan_frontier_items f
  WHERE f.listing_membership_sha256 = source_listing_memberships.sha256
)
"""


def upgrade() -> None:
    """Drop finished scans' board results and superseded scans' frontier checkpoints."""
    postgres = op.get_bind().dialect.name == "postgresql"
    op.execute(_POSTGRES_FINISHED_RESULTS if postgres else _SQLITE_FINISHED_RESULTS)
    op.execute(_SUPERSEDED_CHECKPOINTS)
    op.execute(_UNNAMED_MEMBERSHIPS)


def downgrade() -> None:
    """Released board results and checkpoints cannot be restored."""
