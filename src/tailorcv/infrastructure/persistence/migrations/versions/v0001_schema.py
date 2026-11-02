# ruff: noqa
"""Initial TailorCV schema."""

from collections.abc import Sequence
from datetime import UTC, datetime
import sqlalchemy as sa
from alembic import op

from tailorcv.infrastructure.persistence.types import UTCDateTime

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enforce immutable provenance rows in the database, not only in the mapper.
# Source scans are mutable daily projections; task events retain intraday execution history.
IMMUTABLE_TABLES = (
    "application_events",
    "approval_decisions",
    "audit_events",
    "batch_members",
    "batch_runs",
    "compensation_components",
    "cost_records",
    "eligibility_records",
    "evaluation_currentness_reviews",
    "evaluation_factor_evidence",
    "evaluation_factors",
    "evaluation_score_evidence",
    "evaluation_scores",
    "guardrail_versions",
    "guided_campaign_admitted_discoveries",
    "guided_campaign_branch_outcomes",
    "guided_campaign_branches",
    "guided_campaign_url_observations",
    "guided_campaigns",
    "job_evaluations",
    "job_search_parameter_sets",
    "learning_loop_policy_versions",
    "match_evidence",
    "model_calls",
    "posting_observation_diffs",
    "posting_observations",
    "pending_posting_contents",
    "posting_refusals",
    "pending_discovery_disposition_events",
    "prompt_versions",
    "response_cache_archive",
    "review_proposals",
    "run_prompt_versions",
    "score_weight_components",
    "score_weight_sets",
    "tailoring_runs",
    "task_events",
)


# The default scoring policy includes evidence at weight zero.
DEFAULT_WEIGHT_SET_ID = "score-weights-default-v1"
# This digest is recomputed by the score-policy tests.
DEFAULT_WEIGHT_DIGEST = "a45fc51079eb2ab939bda08582fb6cacbe4e05e3b2bb24e8bf086bb56e2cec51"
DEFAULT_WEIGHTS = (
    ("fit", "0.40"),
    ("compensation", "0.25"),
    ("eligibility", "0.25"),
    ("evidence", "0.00"),
    ("opportunity", "0.10"),
)


def _protect_immutable_tables() -> None:
    """Enforce the provenance boundary even for writes outside the ORM."""
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in IMMUTABLE_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER prevent_{table}_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is immutable');
                END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER prevent_{table}_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is immutable');
                END
                """
            )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tailorcv_reject_immutable_write() RETURNS trigger AS $$
            BEGIN
                -- 23514 is check_violation, so both dialects reach the API as
                -- IntegrityError. A bare RAISE would arrive as InternalError.
                RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
                    USING ERRCODE = '23514';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in IMMUTABLE_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER prevent_{table}_write
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION tailorcv_reject_immutable_write()
                """
            )


def _remove_immutable_protection() -> None:
    """Remove the explicit PostgreSQL triggers before dropping their tables."""
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS prevent_{table}_update")
            op.execute(f"DROP TRIGGER IF EXISTS prevent_{table}_delete")
    elif dialect == "postgresql":
        for table in IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS prevent_{table}_write ON {table}")
        op.execute("DROP FUNCTION IF EXISTS tailorcv_reject_immutable_write()")


def _create_chain_validator() -> None:
    """Refuse an evaluation version that does not continue its own chain.

    A unique constraint stops two version threes but not a version three that
    supersedes a different posting's version one.
    """
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER validate_job_evaluation_chain
            BEFORE INSERT ON job_evaluations
            WHEN NEW.version_number > 1
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1
                    FROM job_evaluations AS previous
                    WHERE previous.id = NEW.supersedes_evaluation_id
                      AND previous.job_posting_id = NEW.job_posting_id
                      AND previous.version_number = NEW.version_number - 1
                ) THEN RAISE(ABORT, 'invalid evaluation supersession chain') END;
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tailorcv_validate_evaluation_chain()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.version_number > 1 AND NOT EXISTS (
                    SELECT 1
                    FROM job_evaluations AS previous
                    WHERE previous.id = NEW.supersedes_evaluation_id
                      AND previous.job_posting_id = NEW.job_posting_id
                      AND previous.version_number = NEW.version_number - 1
                ) THEN
                    RAISE EXCEPTION 'invalid evaluation supersession chain'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER validate_job_evaluation_chain
            BEFORE INSERT ON job_evaluations
            FOR EACH ROW EXECUTE FUNCTION tailorcv_validate_evaluation_chain()
            """
        )


def _drop_chain_validator() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS validate_job_evaluation_chain")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS validate_job_evaluation_chain ON job_evaluations")
        op.execute("DROP FUNCTION IF EXISTS tailorcv_validate_evaluation_chain()")


def _create_campaign_attempt_transition_validator() -> None:
    """Refuse an attempt state change the ledger's own methods never make.

    An uncertain attempt may only be explicitly abandoned; nothing may leave
    a processed or an abandoned attempt. The CHECK constraints on this table
    police one row's own columns, but a transition is a relationship between
    the old and new row, which only a trigger can see.
    """
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER validate_guided_campaign_attempt_transition
            BEFORE UPDATE ON guided_campaign_attempts
            WHEN NEW.state != OLD.state
            BEGIN
                SELECT CASE
                    WHEN OLD.state = 'processed'
                        THEN RAISE(ABORT, 'a processed attempt cannot change state')
                    WHEN OLD.state = 'abandoned'
                        THEN RAISE(ABORT, 'an abandoned attempt cannot change state')
                    WHEN OLD.state = 'uncertain' AND NEW.state != 'abandoned'
                        THEN RAISE(ABORT, 'an uncertain attempt may only be abandoned')
                END;
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tailorcv_validate_campaign_attempt_transition() RETURNS trigger AS $$
            BEGIN
                IF NEW.state = OLD.state THEN
                    RETURN NEW;
                ELSIF OLD.state = 'processed' THEN
                    RAISE EXCEPTION 'a processed attempt cannot change state'
                        USING ERRCODE = '23514';
                ELSIF OLD.state = 'abandoned' THEN
                    RAISE EXCEPTION 'an abandoned attempt cannot change state'
                        USING ERRCODE = '23514';
                ELSIF OLD.state = 'uncertain' AND NEW.state != 'abandoned' THEN
                    RAISE EXCEPTION 'an uncertain attempt may only be abandoned'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER validate_guided_campaign_attempt_transition
            BEFORE UPDATE ON guided_campaign_attempts
            FOR EACH ROW EXECUTE FUNCTION tailorcv_validate_campaign_attempt_transition()
            """
        )


def _drop_campaign_attempt_transition_validator() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS validate_guided_campaign_attempt_transition")
    elif dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS validate_guided_campaign_attempt_transition "
            "ON guided_campaign_attempts"
        )
        op.execute("DROP FUNCTION IF EXISTS tailorcv_validate_campaign_attempt_transition()")


def _seed_default_score_policy() -> None:
    """Insert the one policy that has to exist before anything can be scored.

    Scoped to no workspace, so it is a fallback rather than somebody's choice.
    """
    timestamp = datetime(2026, 8, 6, tzinfo=UTC)
    audit = {
        "created_at": timestamp,
        "created_by": "migration",
        "origin": "score_policy_seed",
        "correlation_id": None,
    }
    weight_sets = sa.table(
        "score_weight_sets",
        sa.column("workspace_id", sa.String(length=36)),
        sa.column("name", sa.String(length=240)),
        sa.column("version", sa.Integer()),
        sa.column("content_sha256", sa.String(length=64)),
        sa.column("effective_at", UTCDateTime()),
        sa.column("id", sa.String(length=36)),
        sa.column("created_at", UTCDateTime()),
        sa.column("created_by", sa.String(length=120)),
        sa.column("origin", sa.String(length=120)),
        sa.column("correlation_id", sa.String(length=120)),
    )
    op.bulk_insert(
        weight_sets,
        [
            {
                "workspace_id": None,
                "name": "TailorCV default",
                "version": 1,
                "content_sha256": DEFAULT_WEIGHT_DIGEST,
                "effective_at": timestamp,
                "id": DEFAULT_WEIGHT_SET_ID,
                **audit,
            }
        ],
    )
    weight_components = sa.table(
        "score_weight_components",
        sa.column("weight_set_id", sa.String(length=36)),
        sa.column("component", sa.String(length=40)),
        sa.column("weight", sa.Numeric(precision=7, scale=6)),
        sa.column("id", sa.String(length=36)),
        sa.column("created_at", UTCDateTime()),
        sa.column("created_by", sa.String(length=120)),
        sa.column("origin", sa.String(length=120)),
        sa.column("correlation_id", sa.String(length=120)),
    )
    op.bulk_insert(
        weight_components,
        [
            {
                "weight_set_id": DEFAULT_WEIGHT_SET_ID,
                "component": component,
                "weight": weight,
                "id": f"score-weight-default-{position}",
                **audit,
            }
            for position, (component, weight) in enumerate(DEFAULT_WEIGHTS, 1)
        ],
    )


def upgrade() -> None:
    """Create every table, protect the immutable ones, and seed the default policy."""
    op.create_table(
        "workspaces",
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=True),
        sa.Column("settings_seed", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("scan_schedule", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )
    op.create_table(
        "audit_events",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_workspace_time", "audit_events", ["workspace_id", "created_at"], unique=False
    )
    op.create_table(
        "worker_presence",
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("heartbeat_at", UTCDateTime(), nullable=False),
        sa.Column("available_until", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_table(
        "background_tasks",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("leased_by", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", UTCDateTime(), nullable=True),
        sa.Column("heartbeat_at", UTCDateTime(), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("cancellation_requested_at", UTCDateTime(), nullable=True),
        sa.Column("error_category", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("completed_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'needs_review', 'committing', 'completed', 'failed', 'cancelled')",
            name="ck_task_status",
        ),
        sa.CheckConstraint(
            "task_type IN ('artifact.verify', 'posting.refresh', 'posting.refresh_batch', "
            "'posting.refresh_with_reextraction', 'posting.refresh_with_rematch', "
            "'posting.recover_batch', 'discovery.evaluate', "
            "'discovery.evaluate_batch', 'discovery.refresh_batch', 'discovery.rescreen_queue', "
            "'discovery.relevance_review', "
            "'workflow.tailor', "
            "'workflow.approve', "
            "'workflow.batch', 'workflow.discovery', 'document.render', 'source.scan', "
            "'source.continue', "
            "'search.guided_campaign', 'search.guided_campaign_retry', 'search.vocabulary', "
            "'posting.resolve_batch')",
            name="ck_task_type",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "task_type", "idempotency_key", name="uq_task_key"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_task_workspace_id"),
    )
    op.create_index(
        "ix_task_claim",
        "background_tasks",
        ["status", "lease_expires_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_workspace_type_status",
        "background_tasks",
        ["workspace_id", "task_type", "status"],
        unique=False,
    )
    op.create_index(
        "ix_task_type_started",
        "background_tasks",
        ["task_type", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_workspace_type_correlation",
        "background_tasks",
        ["workspace_id", "task_type", "correlation_id"],
        unique=False,
    )
    op.create_table(
        "candidate_profiles",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=240), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_candidate_workspace"),
    )
    op.create_table(
        "employers",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("careers_url", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "normalized_name", name="uq_employer"),
    )
    op.create_table(
        "job_search_parameter_sets",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("effective_at", UTCDateTime(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["job_search_parameter_sets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "version", name="uq_job_search_parameter_version"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_job_search_parameter_workspace_id"),
    )
    op.create_table(
        "guardrail_versions",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("effective_at", UTCDateTime(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["guardrail_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "version", name="uq_guardrail_version"),
    )
    op.create_table(
        "learning_loop_policy_versions",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("effective_at", UTCDateTime(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["learning_loop_policy_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "version", name="uq_learning_loop_policy_version"),
    )
    op.create_table(
        "notes",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pending_discoveries",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("employer", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=False),
        sa.Column("staged_facts", sa.JSON(), nullable=False),
        sa.Column("requisition_id", sa.String(length=300), nullable=False),
        sa.Column("discovery_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column(
            "content_status",
            sa.String(length=40),
            server_default="not_yet_read",
            nullable=False,
        ),
        sa.Column("content_note", sa.Text(), server_default="", nullable=False),
        sa.Column("current_content_id", sa.String(length=36), nullable=True),
        sa.Column("listing_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("first_seen_on", sa.Date(), nullable=False),
        sa.Column("withdrawn_at", UTCDateTime(), nullable=True),
        sa.Column("absent_since", UTCDateTime(), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("regional_variant_of", sa.String(length=36), nullable=True),
        sa.Column("favorite", sa.Boolean(), nullable=False),
        sa.Column("kept", sa.Boolean(), nullable=False),
        sa.Column("keep_note", sa.String(length=300), nullable=True),
        sa.Column("closing_date_override", sa.Date(), nullable=True),
        sa.Column("closing_date_override_updated_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.ForeignKeyConstraint(
            ["regional_variant_of"],
            ["pending_discoveries.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
        ),
        sa.ForeignKeyConstraint(
            ["listing_artifact_id"],
            ["artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "id", "current_content_id"],
            [
                "pending_posting_contents.workspace_id",
                "pending_posting_contents.pending_discovery_id",
                "pending_posting_contents.id",
            ],
            name="fk_pending_current_content",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "content_status IN ('readable', 'not_yet_read', 'access_blocked', "
            "'session_required', 'render_required', 'policy_refused', 'fetch_failed')",
            name="ck_pending_content_status",
        ),
        sa.CheckConstraint("keep_note IS NULL OR kept", name="ck_pending_keep_note_needs_keep"),
        sa.UniqueConstraint("workspace_id", "fingerprint", name="uq_pending_fingerprint"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_pending_workspace_id"),
    )
    op.create_table(
        "pending_posting_contents",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("requested_url_sha256", sa.String(length=64), nullable=False),
        sa.Column("final_url_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("place_blind_digest", sa.String(length=12), nullable=True),
        sa.Column("content_status", sa.String(length=40), nullable=False),
        sa.Column(
            "identity_status",
            sa.String(length=40),
            server_default="not_yet_verified",
            nullable=False,
        ),
        sa.Column("posting_status", sa.String(length=20), nullable=True),
        sa.Column("extraction_method", sa.String(length=40), nullable=True),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("publisher_markup_present", sa.Boolean(), nullable=True),
        sa.Column("identity_evidence", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("supersedes_content_id", sa.String(length=36), nullable=True),
        sa.Column("robots_basis", sa.String(length=40), nullable=True),
        sa.Column("publisher_closing_date", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "robots_basis IS NULL OR robots_basis IN ('allowed', 'single_posting_read')",
            name="ck_pending_content_robots_basis",
        ),
        sa.CheckConstraint(
            "content_status IN ('readable', 'not_yet_read', 'access_blocked', "
            "'session_required', 'render_required', 'policy_refused', 'fetch_failed')",
            name="ck_pending_posting_content_status",
        ),
        sa.CheckConstraint(
            "identity_status IN ('not_yet_verified', 'publisher_listed', 'verified', "
            "'ambiguous', 'contradicted')",
            name="ck_pending_posting_identity_status",
        ),
        sa.CheckConstraint(
            "extraction_method IS NULL OR extraction_method IN "
            "('static_structured', 'static_html', 'rendered_html', 'rendered_json', "
            "'user_supplied_text')",
            name="ck_pending_content_extraction_method",
        ),
        sa.CheckConstraint(
            "posting_status IS NULL OR posting_status IN ('active', 'closed')",
            name="ck_pending_content_posting_status",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_pending_content_http_status",
        ),
        sa.CheckConstraint(
            "length(requested_url_sha256) = 64 AND "
            "(final_url_sha256 IS NULL OR length(final_url_sha256) = 64) AND "
            "(content_sha256 IS NULL OR length(content_sha256) = 64)",
            name="ck_pending_content_digest_lengths",
        ),
        sa.CheckConstraint(
            "(final_url IS NULL AND final_url_sha256 IS NULL) OR "
            "(length(trim(final_url)) > 0 AND final_url_sha256 IS NOT NULL)",
            name="ck_pending_content_final_url_digest",
        ),
        sa.CheckConstraint(
            "length(trim(requested_url)) > 0", name="ck_pending_content_requested_url"
        ),
        sa.CheckConstraint(
            "length(identity_evidence) <= 16384", name="ck_pending_content_identity_evidence"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_pending_content_owner",
        ),
        sa.ForeignKeyConstraint(["supersedes_content_id"], ["pending_posting_contents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "pending_discovery_id",
            "id",
            name="uq_pending_content_owner_id",
        ),
        sa.UniqueConstraint("supersedes_content_id", name="uq_pending_content_supersedes"),
    )
    op.create_index(
        "ix_pending_content_history",
        "pending_posting_contents",
        ["pending_discovery_id", "fetched_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "pending_discovery_disposition_events",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("restore_provenance", sa.String(length=20), nullable=True),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("duplicate_of_job_id", sa.String(length=36), nullable=True),
        sa.Column("duplicate_of_pending_id", sa.String(length=36), nullable=True),
        sa.Column("selection_mode", sa.String(length=20), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("search_parameter_set_id", sa.String(length=36), nullable=True),
        sa.Column("screening", sa.JSON(), nullable=False),
        sa.Column("supersedes_event_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "action IN ('dismiss', 'restore')", name="ck_pending_disposition_action"
        ),
        sa.CheckConstraint(
            "(action = 'dismiss' AND restore_provenance IS NULL) OR "
            "(action = 'restore' AND restore_provenance IS NOT NULL "
            "AND restore_provenance IN ('person', 'audit'))",
            name="ck_pending_disposition_restore_provenance",
        ),
        sa.CheckConstraint(
            "duplicate_of_pending_id IS NULL OR "
            "(action = 'dismiss' AND selection_mode = 'explicit' "
            "AND category = 'duplicate_pending' "
            "AND duplicate_of_pending_id != pending_discovery_id)",
            name="ck_pending_disposition_pending_duplicate",
        ),
        sa.CheckConstraint(
            "duplicate_of_job_id IS NULL OR "
            "(action = 'dismiss' AND selection_mode = 'explicit' "
            "AND category = 'duplicate_posting')",
            name="ck_pending_disposition_job_duplicate",
        ),
        sa.CheckConstraint(
            "duplicate_of_job_id IS NULL OR duplicate_of_pending_id IS NULL",
            name="ck_pending_disposition_one_duplicate_reference",
        ),
        sa.CheckConstraint(
            "action != 'dismiss' OR selection_mode != 'explicit' OR "
            "category != 'duplicate_pending' OR duplicate_of_pending_id IS NOT NULL",
            name="ck_pending_disposition_pending_duplicate_required",
        ),
        sa.CheckConstraint(
            "action != 'dismiss' OR selection_mode != 'explicit' OR "
            "category != 'duplicate_posting' OR duplicate_of_job_id IS NOT NULL",
            name="ck_pending_disposition_job_duplicate_required",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["pending_discovery_id"], ["pending_discoveries.id"]),
        sa.ForeignKeyConstraint(["duplicate_of_job_id"], ["job_postings.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "duplicate_of_pending_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_pending_disposition_duplicate_workspace",
        ),
        sa.ForeignKeyConstraint(["search_parameter_set_id"], ["job_search_parameter_sets.id"]),
        sa.ForeignKeyConstraint(
            ["supersedes_event_id"], ["pending_discovery_disposition_events.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("supersedes_event_id", name="uq_pending_disposition_supersedes"),
    )
    op.create_index(
        "ix_pending_disposition_latest",
        "pending_discovery_disposition_events",
        ["pending_discovery_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "prompt_templates",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "key", name="uq_prompt_template"),
    )
    op.create_table(
        "saved_views",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("resource", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "resource", "name", name="uq_saved_view"),
    )
    op.create_table(
        "score_weight_sets",
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("effective_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_sha256", name="uq_score_weight_digest"),
        sa.UniqueConstraint("workspace_id", "name", "version", name="uq_score_weight_version"),
    )
    op.create_table(
        "sources",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("source_type", sa.String(length=80), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("terms_url", sa.Text(), nullable=False),
        sa.Column("terms_condition", sa.Text(), nullable=False),
        sa.Column("resolved_platform", sa.String(length=80), nullable=True),
        sa.Column("resolved_url", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["employer_id"],
            ["employers.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.CheckConstraint(
            "source_type IN ('employer_board', 'aggregator')", name="ck_source_type"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_source_name"),
    )
    op.create_table(
        "tags",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("color", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_tag_name"),
    )
    op.create_table(
        "job_postings",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("normalized_title", sa.String(length=500), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("posting_hint", sa.Text(), nullable=True),
        sa.Column("closing_date_override", sa.Date(), nullable=True),
        sa.Column("closing_date_override_updated_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["employer_id"],
            ["employers.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_employer", "job_postings", ["employer_id"], unique=False)
    op.create_index(
        "ix_job_workspace_title", "job_postings", ["workspace_id", "title"], unique=False
    )
    op.create_table(
        "master_cv_versions",
        sa.Column("candidate_profile_id", sa.String(length=36), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("effective_at", UTCDateTime(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"],
            ["candidate_profiles.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_profile_id", "content_sha256", name="uq_cv_content"),
    )
    op.create_table(
        "master_cv_entries",
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("entry_key", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("role_key", sa.Text(), nullable=True),
        sa.Column("group", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["master_cv_version_id"],
            ["master_cv_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("master_cv_version_id", "entry_id", name="uq_cv_entry_identity"),
        sa.UniqueConstraint("master_cv_version_id", "entry_key", name="uq_cv_entry_key"),
    )
    op.create_index(
        "ix_cv_entry_version_position",
        "master_cv_entries",
        ["master_cv_version_id", "position"],
        unique=False,
    )
    op.create_index("ix_cv_entry_key", "master_cv_entries", ["entry_key"], unique=False)
    op.create_table(
        "derived_search_titles",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("normalized_title", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("lane", sa.String(length=40), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=40), nullable=True),
        sa.Column("carried_identity", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "lane IS NULL OR lane IN ('leadership', 'specialist')", name="ck_search_title_lane"
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')", name="ck_search_title_state"
        ),
        sa.CheckConstraint(
            "rejection_reason IS NULL OR rejection_reason IN "
            "('not_relevant', 'wrong_level', 'wrong_mandate', 'wrong_employer_type', "
            "'duplicate', 'defer', 'other')",
            name="ck_search_title_rejection_reason",
        ),
        sa.ForeignKeyConstraint(
            ["master_cv_version_id"],
            ["master_cv_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "master_cv_version_id", "normalized_title", name="uq_derived_title"
        ),
    )
    op.create_table(
        "derived_employer_archetypes",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=120), nullable=False),
        sa.Column("because", sa.String(length=300), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=40), nullable=True),
        sa.Column("carried_identity", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')", name="ck_search_archetype_state"
        ),
        sa.CheckConstraint(
            "rejection_reason IS NULL OR rejection_reason IN "
            "('not_relevant', 'wrong_level', 'wrong_mandate', 'wrong_employer_type', "
            "'duplicate', 'defer', 'other')",
            name="ck_search_archetype_rejection_reason",
        ),
        sa.ForeignKeyConstraint(
            ["master_cv_version_id"],
            ["master_cv_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "master_cv_version_id", "normalized_name", name="uq_derived_archetype"
        ),
    )
    op.create_table(
        "derived_clusters",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=120), nullable=False),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.Column("because", sa.String(length=300), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=40), nullable=True),
        sa.Column("carried_identity", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("kind IN ('capability', 'domain')", name="ck_cluster_kind"),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')", name="ck_cluster_state"
        ),
        sa.CheckConstraint(
            "rejection_reason IS NULL OR rejection_reason IN "
            "('not_relevant', 'wrong_level', 'wrong_mandate', 'wrong_employer_type', "
            "'duplicate', 'defer', 'other')",
            name="ck_cluster_rejection_reason",
        ),
        sa.ForeignKeyConstraint(
            ["master_cv_version_id"],
            ["master_cv_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "master_cv_version_id",
            "kind",
            "normalized_name",
            name="uq_derived_cluster",
        ),
    )
    op.create_table(
        "missed_requirement_texts_v1",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("requirement_text", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["master_cv_version_id"],
            ["master_cv_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_evaluation_id"],
            ["job_evaluations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "master_cv_version_id",
            "requirement_text",
            "source_evaluation_id",
            name="uq_missed_requirement_text",
        ),
    )
    op.create_index(
        "ix_missed_requirement_workspace_cv",
        "missed_requirement_texts_v1",
        ["workspace_id", "master_cv_version_id"],
        unique=False,
    )
    op.create_table(
        "prompt_versions",
        sa.Column("prompt_template_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(length=80), nullable=False),
        sa.Column("base_digest", sa.String(length=80), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("version_number >= 1", name="ck_prompt_version_positive"),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prompt_template_id", "version_number", name="uq_prompt_version"),
    )
    op.create_index(
        "ix_prompt_version_digest",
        "prompt_versions",
        ["prompt_template_id", "content_digest"],
        unique=False,
    )
    op.create_table(
        "score_weight_components",
        sa.Column("weight_set_id", sa.String(length=36), nullable=False),
        sa.Column("component", sa.String(length=80), nullable=False),
        sa.Column("weight", sa.Numeric(precision=7, scale=6), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("weight >= 0 AND weight <= 1", name="ck_score_weight_range"),
        sa.ForeignKeyConstraint(
            ["weight_set_id"],
            ["score_weight_sets.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("weight_set_id", "component", name="uq_score_weight_component"),
    )
    op.create_table(
        "tag_assignments",
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tag_id", "entity_type", "entity_id", name="uq_tag_assignment"),
    )
    op.create_table(
        "task_events",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=120), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["background_tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_task_event"),
    )
    op.create_table(
        "applications",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "job_posting_id", name="uq_application_job"),
    )
    op.create_table(
        "posting_identifiers",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("identifier_type", sa.String(length=80), nullable=False),
        sa.Column("identifier_value", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "identifier_type", "identifier_value", name="uq_posting_identifier"
        ),
    )
    op.create_table(
        "posting_urls",
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("url_type", sa.String(length=40), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_posting_id", "normalized_url", name="uq_posting_url"),
    )
    op.create_table(
        "source_scans",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("scanned_on", sa.Date(), nullable=False),
        sa.Column("query_label", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "read_outcome",
            sa.String(length=40),
            server_default="not_recorded",
            nullable=False,
        ),
        sa.Column("raw_count", sa.Integer(), nullable=False),
        sa.Column("read_count", sa.Integer(), nullable=False),
        sa.Column("skip_reasons", sa.JSON(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("detail_attempted_count", sa.Integer(), nullable=False),
        sa.Column("detail_read_count", sa.Integer(), nullable=False),
        sa.Column("detail_failed_count", sa.Integer(), nullable=False),
        sa.Column("detail_interrupted_count", sa.Integer(), nullable=True),
        sa.Column("detail_capped_count", sa.Integer(), nullable=False),
        sa.Column("detail_failure_reasons", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=True),
        sa.Column("discovery_truncated", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("listing_pages", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("exhausted_outcome", sa.String(length=40), nullable=True),
        sa.Column("detail_coverage", sa.JSON(), nullable=True),
        sa.Column("scan_started_at", UTCDateTime(), nullable=False),
        sa.Column("scan_completed_at", UTCDateTime(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("raw_count >= read_count", name="ck_source_scan_raw_covers_read"),
        sa.CheckConstraint(
            "read_outcome IN ('not_recorded', 'ats_complete', 'publisher_index', "
            "'publisher_linked_pages', 'crawl_continuation', 'access_blocked', "
            "'session_required', 'render_required', 'policy_refused', "
            "'no_supported_representation', 'fetch_failed')",
            name="ck_source_scan_read_outcome",
        ),
        sa.CheckConstraint(
            "exhausted_outcome IN ('not_recorded', 'ats_complete', 'publisher_index', "
            "'publisher_linked_pages', 'crawl_continuation', 'access_blocked', "
            "'session_required', 'render_required', 'policy_refused', "
            "'no_supported_representation', 'fetch_failed')",
            name="ck_source_scan_exhausted_outcome",
        ),
        sa.CheckConstraint(
            "detail_interrupted_count IS NULL OR "
            "detail_attempted_count = detail_read_count + detail_failed_count "
            "+ detail_interrupted_count",
            name="ck_source_scan_detail_outcome",
        ),
        sa.CheckConstraint(
            "detail_interrupted_count IS NULL OR detail_interrupted_count >= 0",
            name="ck_source_scan_detail_interrupted_nonnegative",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_source_scan_duration_nonnegative"),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "scanned_on", "query_label", name="uq_source_scan"),
    )
    op.create_table(
        "source_scan_continuations",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("batch_member_id", sa.String(length=36), nullable=True),
        sa.Column("source_scan_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("handoff_scopes", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["source_scan_id"], ["source_scans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("generation >= 1", name="ck_source_continuation_generation"),
        sa.ForeignKeyConstraint(["batch_member_id"], ["source_scan_members.id"]),
        sa.UniqueConstraint("source_scan_id", name="uq_source_scan_continuation"),
    )
    op.create_table(
        "source_scan_members",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("root_task_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("source_scan_id", sa.String(length=36), nullable=True),
        sa.Column("continuation_id", sa.String(length=36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("cancellation_requested_at", UTCDateTime(), nullable=True),
        sa.Column("released_at", UTCDateTime(), nullable=True),
        sa.Column("active_task_id", sa.String(length=36), nullable=True),
        sa.Column("active_attempt_count", sa.Integer(), nullable=True),
        sa.Column("active_process_id", sa.Integer(), nullable=True),
        sa.Column("active_process_started_at", sa.String(length=200), nullable=True),
        sa.Column("read_started_at", UTCDateTime(), nullable=True),
        sa.Column("read_completed_at", UTCDateTime(), nullable=True),
        sa.Column("error_category", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["root_task_id"], ["background_tasks.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["source_scan_id"], ["source_scans.id"]),
        sa.ForeignKeyConstraint(
            ["continuation_id"],
            ["source_scan_continuations.id"],
            name="fk_source_scan_member_continuation",
        ),
        sa.ForeignKeyConstraint(["active_task_id"], ["background_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root_task_id", "source_id", name="uq_source_scan_member"),
        sa.CheckConstraint("ordinal >= 0", name="ck_source_scan_member_ordinal"),
        sa.CheckConstraint(
            "active_attempt_count IS NULL OR active_attempt_count >= 1",
            name="ck_source_scan_member_attempt",
        ),
        sa.CheckConstraint(
            "generation IS NULL OR generation >= 1", name="ck_source_scan_member_generation"
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'scanning', 'continuing', 'completed', 'failed', 'cancelled', 'already_in_progress')",
            name="ck_source_scan_member_state",
        ),
    )
    op.create_index(
        "uq_source_scan_member_reserved_source",
        "source_scan_members",
        ["source_id"],
        unique=True,
        sqlite_where=sa.text("released_at IS NULL"),
    )
    op.create_table(
        "source_scan_outcomes",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("source_scan_id", sa.String(length=36), nullable=True),
        sa.Column("source_name", sa.String(length=300), nullable=False),
        sa.Column("candidate_kind", sa.String(length=40), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("employer", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=False),
        sa.Column("terminal_stage", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=False),
        sa.Column("screening_findings", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["background_tasks.id"]),
        sa.ForeignKeyConstraint(["source_scan_id"], ["source_scans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "source_name",
            "candidate_kind",
            "normalized_url",
            name="uq_source_scan_outcome",
        ),
        sa.CheckConstraint(
            "candidate_kind IN ('direct_posting', 'board_lead', 'board_listing')",
            name="ck_source_scan_outcome_kind",
        ),
        sa.CheckConstraint(
            "terminal_stage IN ('proposed', 'fetched', 'read', 'screened', 'staged', 'rejected')",
            name="ck_source_scan_outcome_stage",
        ),
        sa.CheckConstraint(
            "reason IN ('staged', 'already_known', 'incomplete_posting', 'duplicate_url', "
            "'duplicate_lead', 'excluded_employer', 'hard_constraint_excluded', "
            "'insufficient_title_evidence', 'seniority_outside_band', "
            "'location_outside_saved_places', "
            "'stage_limit_reached', 'role_family_review_limit_reached', 'no_title_configured', "
            "'no_open_positions', 'no_matching_listings', 'no_new_openings', 'fetch_refused', "
            "'fetch_failed', 'no_readable_board')",
            name="ck_source_scan_outcome_reason",
        ),
    )
    op.create_index(
        "ix_source_scan_outcome_workspace_task",
        "source_scan_outcomes",
        ["workspace_id", "task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_scan_outcome_workspace_reason",
        "source_scan_outcomes",
        ["workspace_id", "reason", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_scan_outcome_workspace_url",
        "source_scan_outcomes",
        ["workspace_id", "normalized_url"],
        unique=False,
    )
    op.create_index(
        "ix_source_scan_outcome_source_scan",
        "source_scan_outcomes",
        ["source_scan_id"],
        unique=False,
    )
    op.create_index(
        "ix_source_scan_outcome_history",
        "source_scan_outcomes",
        ["workspace_id", "task_id", "source_name", "reason", "normalized_url"],
        unique=False,
    )
    op.create_table(
        "screened_out_titles",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=300), nullable=False),
        sa.Column("employer", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("title_code", sa.String(length=80), nullable=False),
        sa.Column("title_subject", sa.String(length=500), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.Column("reviewed_at", UTCDateTime(), nullable=True),
        sa.Column("reviewed_title_code", sa.String(length=80), nullable=True),
        sa.Column("reviewed_title_subject", sa.String(length=500), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "normalized_url", name="uq_screened_out_title"),
    )
    op.create_index(
        "ix_screened_out_title_last_seen",
        "screened_out_titles",
        ["workspace_id", "last_seen_at"],
        unique=False,
    )
    op.create_table(
        "source_listing_memberships",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("membership", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("sha256"),
    )
    op.create_table(
        "source_scan_frontier_items",
        sa.Column("continuation_id", sa.String(length=36), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("label_source", sa.String(length=20), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("structural_context", sa.String(length=20), nullable=False),
        sa.Column("relations", sa.JSON(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("automatic_eligible", sa.Boolean(), nullable=False),
        sa.Column("deferred_reason", sa.String(length=120), nullable=False),
        sa.Column("detail_checkpoint", sa.JSON(), nullable=True),
        sa.Column("listing_membership_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("completed_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed')", name="ck_source_frontier_status"
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_source_frontier_attempt_count"),
        sa.CheckConstraint("depth >= 1", name="ck_source_frontier_depth"),
        sa.ForeignKeyConstraint(["continuation_id"], ["source_scan_continuations.id"]),
        sa.ForeignKeyConstraint(
            ["listing_membership_sha256"], ["source_listing_memberships.sha256"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("continuation_id", "normalized_url", name="uq_source_frontier_url"),
    )
    op.create_index(
        "ix_source_frontier_work",
        "source_scan_frontier_items",
        ["continuation_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_frontier_listing_membership",
        "source_scan_frontier_items",
        ["listing_membership_sha256"],
        unique=False,
    )
    op.create_table(
        "posting_refusals",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("job_posting_id", sa.String(length=36), nullable=True),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("search_parameter_set_id", sa.String(length=36), nullable=True),
        sa.Column("employer", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("condition", sa.String(length=60), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("refused_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["job_posting_id"], ["job_postings.id"]),
        sa.ForeignKeyConstraint(["pending_discovery_id"], ["pending_discoveries.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["background_tasks.id"]),
        sa.ForeignKeyConstraint(["search_parameter_set_id"], ["job_search_parameter_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "refusal_pattern_proposals",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("condition", sa.String(length=60), nullable=False),
        sa.Column("normalized_reason", sa.Text(), nullable=False),
        sa.Column("reason_examples", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.Column("refusal_count", sa.Integer(), nullable=False),
        sa.Column("proposed_at", UTCDateTime(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')",
            name="ck_refusal_pattern_proposal_state",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "condition", "normalized_reason", name="uq_refusal_pattern_proposal"
        ),
    )
    op.create_table(
        "master_cv_proposals",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("requirement_key", sa.String(length=36), nullable=False),
        sa.Column("requirement_text", sa.Text(), nullable=False),
        sa.Column("evidence_note", sa.Text(), nullable=True),
        sa.Column("posting_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.Column("proposed_at", UTCDateTime(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "kind IN ('promote_transferable', 'add_missing')", name="ck_master_cv_proposal_kind"
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')", name="ck_master_cv_proposal_state"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "kind", "requirement_key", name="uq_master_cv_proposal_identity"
        ),
    )
    op.create_table(
        "tailoring_runs",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("job_posting_id", sa.String(length=36), nullable=True),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("outcome", sa.String(length=80), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("completed_at", UTCDateTime(), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('passed', 'incomplete', 'checks failed')", name="ck_tailoring_outcome"
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["master_cv_version_id"],
            ["master_cv_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["background_tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "task_id", name="uq_tailoring_run_task"),
    )
    op.create_table(
        "application_events",
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("source_timestamp", sa.String(length=80), nullable=True),
        sa.Column("corrects_event_id", sa.String(length=36), nullable=True),
        sa.Column("reverses_event_id", sa.String(length=36), nullable=True),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('status', 'correction', 'reversal')", name="ck_event_type"
        ),
        sa.CheckConstraint(
            "status IN ('not_applied', 'package_ready', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn', 'declined', 'closed_before_application')",
            name="ck_application_status",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
        ),
        sa.ForeignKeyConstraint(
            ["corrects_event_id"],
            ["application_events.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reverses_event_id"],
            ["application_events.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "idempotency_key", name="uq_application_event_key"),
    )
    op.create_index(
        "ix_application_event_time",
        "application_events",
        ["application_id", "occurred_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "artifacts",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("logical_type", sa.String(length=100), nullable=False),
        sa.Column("mime_type", sa.String(length=160), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("related_entity_type", sa.String(length=80), nullable=True),
        sa.Column("related_entity_id", sa.String(length=36), nullable=True),
        sa.Column("tailoring_run_id", sa.String(length=36), nullable=True),
        sa.Column("artifact_family_id", sa.String(length=36), nullable=True),
        sa.Column("version_number", sa.Integer(), nullable=True),
        sa.Column("supersedes_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("author_type", sa.String(length=24), nullable=False),
        sa.Column("last_verified_at", UTCDateTime(), nullable=True),
        sa.Column("integrity_status", sa.String(length=40), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("archived_at", UTCDateTime(), nullable=True),
        sa.CheckConstraint(
            "logical_type IN ('posting.body.text', 'posting.body.html', "
            "'posting.listing.text', 'posting.source.json', 'posting.source.html', "
            "'posting.source.xml', 'posting.source.pdf', 'posting.source.image', "
            "'master_cv.snapshot.markdown', 'master_cv.snapshot.text', "
            "'evaluation.fit_report.markdown', "
            "'screening.report.markdown', 'batch.report.markdown', 'batch.record.json', "
            "'discovery.session.json', 'resume.markdown', 'cover_letter.markdown', "
            "'tailoring.report.markdown', 'tailoring.manifest.json', "
            "'tailoring.review.json', 'tailoring.review.markdown', "
            "'company_research.posting.text', 'company_research.careers.text', "
            "'company_research.about.text', 'document.rendered.pdf', "
            "'document.rendered.text', 'user.attachment', 'recruiter.email', 'take_home')",
            name="ck_artifact_logical_type",
        ),
        sa.CheckConstraint(
            "author_type IN ('human', 'model', 'system')", name="ck_artifact_author_type"
        ),
        sa.CheckConstraint(
            "integrity_status IN ('unverified', 'staged', 'verified', 'unresolved', "
            "'missing', 'changed')",
            name="ck_artifact_integrity_status",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_artifact_id"],
            ["artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tailoring_run_id"],
            ["tailoring_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "artifact_family_id",
            "version_number",
            name="uq_artifact_family_version",
        ),
        sa.UniqueConstraint("workspace_id", "storage_uri", "sha256", name="uq_artifact_version"),
    )
    op.create_index(
        "ix_artifact_family",
        "artifacts",
        ["workspace_id", "artifact_family_id", "version_number"],
        unique=False,
    )
    op.create_index("ix_artifact_type", "artifacts", ["workspace_id", "logical_type"], unique=False)
    op.create_table(
        "review_proposals",
        sa.Column("tailoring_run_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_key", sa.String(length=160), nullable=False),
        sa.Column("slot", sa.String(length=300), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["tailoring_run_id"],
            ["tailoring_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tailoring_run_id", "proposal_key", name="uq_proposal"),
    )
    op.create_table(
        "approval_decisions",
        sa.Column("review_proposal_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column("replacement_text", sa.Text(), nullable=True),
        sa.Column("replacement_source_ids", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("decided_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["review_proposal_id"],
            ["review_proposals.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_proposal_id", "manifest_sha256", name="uq_approval"),
    )
    op.create_table(
        "batch_runs",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("result_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("completed_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["result_artifact_id"],
            ["artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["background_tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "posting_observations",
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("observed_at", UTCDateTime(), nullable=False),
        sa.Column("posting_status", sa.String(length=40), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=False),
        sa.Column("residence_rule", sa.Text(), nullable=False),
        sa.Column("employment_type", sa.String(length=160), nullable=False),
        sa.Column("payroll_arrangement", sa.Text(), nullable=False),
        sa.Column("body_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("extraction_method", sa.String(length=80), nullable=True),
        sa.Column("fetched_at", UTCDateTime(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("robots_basis", sa.String(length=40), nullable=True),
        sa.Column("publisher_closing_date", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_observation_http_status",
        ),
        sa.CheckConstraint(
            "robots_basis IS NULL OR robots_basis IN ('allowed', 'single_posting_read')",
            name="ck_observation_robots_basis",
        ),
        sa.CheckConstraint("posting_status IN ('active', 'closed')", name="ck_posting_status"),
        sa.ForeignKeyConstraint(
            ["body_artifact_id"],
            ["artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_observation_job_observed",
        "posting_observations",
        ["job_posting_id", "observed_at"],
        unique=False,
    )
    op.create_table(
        "run_prompt_versions",
        sa.Column("prompt_version_id", sa.String(length=36), nullable=False),
        sa.Column("tailoring_run_id", sa.String(length=36), nullable=True),
        sa.Column("batch_run_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "(tailoring_run_id IS NOT NULL AND batch_run_id IS NULL) OR (tailoring_run_id IS NULL AND batch_run_id IS NOT NULL)",
            name="ck_run_prompt_one_run",
        ),
        sa.ForeignKeyConstraint(
            ["batch_run_id"],
            ["batch_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tailoring_run_id"],
            ["tailoring_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_run_id", "prompt_version_id", name="uq_batch_prompt_version"),
        sa.UniqueConstraint("tailoring_run_id", "prompt_version_id", name="uq_run_prompt_version"),
    )
    op.create_table(
        "batch_members",
        sa.Column("batch_run_id", sa.String(length=36), nullable=False),
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_run_id"],
            ["batch_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_run_id", "job_posting_id", name="uq_batch_member"),
    )
    op.create_table(
        "compensation_components",
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=True),
        sa.Column("component_type", sa.String(length=80), nullable=False),
        sa.Column("amount_min", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("amount_max", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("annualized_min", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("annualized_max", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("basis", sa.String(length=12), nullable=False),
        sa.Column("geography", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["posting_observations.id"],
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency NOT GLOB '*[^A-Z]*'", name="ck_compensation_currency"
        ),
        sa.CheckConstraint(
            "basis IN ('annual', 'monthly', 'daily', 'hourly')", name="ck_compensation_basis"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eligibility_records",
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_location_key", sa.String(length=80), nullable=False),
        sa.Column("region_rule", sa.String(length=160), nullable=False),
        sa.Column("arrangement", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["posting_observations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "job_evaluations",
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_evaluation_id", sa.String(length=36), nullable=True),
        sa.Column("observation_id", sa.String(length=36), nullable=True),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=True),
        sa.Column("score_weight_set_id", sa.String(length=36), nullable=True),
        sa.Column("search_parameter_set_id", sa.String(length=36), nullable=True),
        sa.Column("fit_report_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("fit_report_payload", sa.JSON(), nullable=True),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("scope_summary", sa.Text(), nullable=False),
        sa.Column("qualifications_summary", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("evaluated_at", UTCDateTime(), nullable=False),
        sa.Column("source_timestamp", sa.String(length=80), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "decision IN ('apply_now', 'premium_target', 'investigate', 'monitor', 'deprioritize', 'ineligible', 'closed_evidence')",
            name="ck_evaluation_decision",
        ),
        sa.CheckConstraint(
            "(version_number = 1 AND supersedes_evaluation_id IS NULL) OR (version_number > 1 AND supersedes_evaluation_id IS NOT NULL)",
            name="ck_evaluation_version_chain",
        ),
        sa.CheckConstraint("version_number >= 1", name="ck_evaluation_version_positive"),
        sa.ForeignKeyConstraint(
            ["fit_report_artifact_id"],
            ["artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["master_cv_version_id"],
            ["master_cv_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["posting_observations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["score_weight_set_id"],
            ["score_weight_sets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["search_parameter_set_id"],
            ["job_search_parameter_sets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_evaluation_id"],
            ["job_evaluations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_posting_id", "version_number", name="uq_job_evaluation_version"),
        sa.UniqueConstraint("supersedes_evaluation_id", name="uq_evaluation_supersedes"),
    )
    op.create_index(
        "ix_evaluation_job_time",
        "job_evaluations",
        ["job_posting_id", "evaluated_at"],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_job_version",
        "job_evaluations",
        ["job_posting_id", "version_number"],
        unique=False,
    )
    op.create_table(
        "evaluation_currentness_reviews",
        sa.Column("evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("posting_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("posting_status", sa.String(length=24), nullable=False),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("search_parameter_set_id", sa.String(length=36), nullable=False),
        sa.Column("reviewed_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["evaluation_id"], ["job_evaluations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_id", "snapshot_sha256", name="uq_evaluation_currentness_review_snapshot"
        ),
    )
    op.create_index(
        "ix_evaluation_currentness_review_evaluation",
        "evaluation_currentness_reviews",
        ["evaluation_id", "reviewed_at"],
        unique=False,
    )
    op.create_table(
        "model_calls",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("tailoring_run_id", sa.String(length=36), nullable=True),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("requested_model", sa.String(length=160), nullable=False),
        sa.Column("served_model", sa.String(length=160), nullable=True),
        sa.Column("provider_response_id", sa.String(length=300), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        # These two count the provider's prompt cache; cache_hit is a different
        # cache, true only when TailorCV replayed a stored answer instead of
        # calling the provider at all.
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("completion_reason", sa.String(length=80), nullable=True),
        sa.Column("effort", sa.String(length=40), nullable=True),
        sa.Column("verbosity", sa.String(length=40), nullable=True),
        sa.Column("image_detail", sa.String(length=40), nullable=True),
        sa.Column("image_token_ceiling", sa.Integer(), nullable=True),
        sa.Column("service_tier", sa.String(length=80), nullable=True),
        sa.Column("resent_on_standard", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("code_revision", sa.String(length=192), nullable=True),
        sa.Column("package_version", sa.String(length=64), nullable=True),
        sa.Column("prompt_names", sa.JSON(), nullable=False),
        sa.Column("instructions_digest", sa.String(length=64), nullable=True),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("task_digest", sa.String(length=64), nullable=True),
        sa.Column("schema_digest", sa.String(length=64), nullable=True),
        sa.Column("tool_policy", sa.String(length=40), nullable=True),
        sa.Column("tool_version", sa.String(length=80), nullable=True),
        sa.Column("search_policy", sa.JSON(), nullable=True),
        sa.Column("searches", sa.JSON(), nullable=False),
        sa.Column("search_request_count", sa.Integer(), nullable=False),
        sa.Column("validated", sa.Boolean(), nullable=False),
        # The response cache key, derived for every call; cached_at is set only
        # on a hit and holds the cache file's own UTC text.
        sa.Column("cache_key", sa.String(length=64), nullable=True),
        sa.Column("cached_at", sa.String(length=40), nullable=True),
        sa.Column("response_digest", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["tailoring_run_id"],
            ["tailoring_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["background_tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "model_call_envelopes",
        sa.Column("model_call_id", sa.String(length=36), nullable=False),
        # The request body carries prompt text including CV evidence.
        sa.Column("request_envelope", sa.JSON(), nullable=True),
        sa.Column("response_envelope", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["model_call_id"], ["model_calls.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_call_id", name="uq_model_call_envelope"),
    )
    op.create_table(
        "response_cache_archive",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        # The cache file's own UTC text stamp rather than a parsed time.
        sa.Column("stored_at", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.String(length=192), nullable=True),
        sa.Column("package_version", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cache_key", "stored_at", name="uq_response_cache_archive"),
    )
    op.create_table(
        "guided_campaigns",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("search_parameter_set_id", sa.String(length=36), nullable=False),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("compiler_version", sa.String(length=80), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_hosted_searches", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("searches_per_attempt", sa.Integer(), nullable=False),
        sa.Column("approved_task_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("cycle >= 1", name="ck_guided_campaign_cycle_positive"),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64", name="ck_guided_campaign_manifest_digest"
        ),
        sa.CheckConstraint("max_model_calls >= 1", name="ck_guided_campaign_max_model_calls"),
        sa.CheckConstraint(
            "max_hosted_searches >= 1", name="ck_guided_campaign_max_hosted_searches"
        ),
        sa.CheckConstraint("max_output_tokens >= 1", name="ck_guided_campaign_max_output_tokens"),
        sa.CheckConstraint(
            "searches_per_attempt >= 1", name="ck_guided_campaign_searches_per_attempt"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "search_parameter_set_id"],
            ["job_search_parameter_sets.workspace_id", "job_search_parameter_sets.id"],
        ),
        sa.ForeignKeyConstraint(["master_cv_version_id"], ["master_cv_versions.id"]),
        sa.ForeignKeyConstraint(["approved_task_id"], ["background_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "manifest_sha256", "cycle", name="uq_guided_campaign_scope_cycle"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_guided_campaign_workspace_id"),
    )
    op.create_table(
        "guided_campaign_states",
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("owner_task_id", sa.String(length=36), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("completed_at", UTCDateTime(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'incomplete', 'paused_uncertain', "
            "'cancelled', 'failed')",
            name="ck_guided_campaign_state_status",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["guided_campaigns.id"]),
        sa.ForeignKeyConstraint(["owner_task_id"], ["background_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", name="uq_guided_campaign_state_campaign"),
    )
    op.create_table(
        "guided_campaign_branches",
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("branch_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("priority_family", sa.String(length=80), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("attempt_position", sa.Integer(), nullable=True),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("position >= 1", name="ck_guided_campaign_branch_position_positive"),
        sa.CheckConstraint("length(branch_key) = 64", name="ck_guided_campaign_branch_key_digest"),
        sa.CheckConstraint(
            "attempt_position IS NULL OR attempt_position >= 1",
            name="ck_guided_campaign_branch_attempt_position",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["guided_campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "position", name="uq_guided_campaign_branch_position"),
        sa.UniqueConstraint("campaign_id", "branch_key", name="uq_guided_campaign_branch_key"),
    )
    op.create_table(
        "guided_campaign_attempts",
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("search_cap", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("provider_response_id", sa.String(length=200), nullable=True),
        sa.Column("request_envelope", sa.JSON(), nullable=True),
        sa.Column("model_call_id", sa.String(length=36), nullable=True),
        sa.Column("hosted_search_count", sa.Integer(), nullable=False),
        sa.Column("page_action_count", sa.Integer(), nullable=False),
        sa.Column("finish", sa.String(length=40), nullable=False),
        sa.Column("validated", sa.Boolean(), nullable=False),
        sa.Column("capture", sa.JSON(), nullable=False),
        sa.Column("error_category", sa.String(length=120), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("dispatched_at", UTCDateTime(), nullable=True),
        sa.Column("captured_at", UTCDateTime(), nullable=True),
        sa.Column("processed_at", UTCDateTime(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("position >= 1", name="ck_guided_campaign_attempt_position_positive"),
        sa.CheckConstraint("search_cap >= 1", name="ck_guided_campaign_attempt_search_cap"),
        sa.CheckConstraint(
            "hosted_search_count >= 0", name="ck_guided_campaign_attempt_hosted_count"
        ),
        sa.CheckConstraint("page_action_count >= 0", name="ck_guided_campaign_attempt_page_count"),
        sa.CheckConstraint(
            "state IN ('planned', 'dispatching', 'dispatched', 'captured', 'processed', "
            "'uncertain', 'abandoned')",
            name="ck_guided_campaign_attempt_state",
        ),
        sa.CheckConstraint(
            "error_category IN ('', 'capacity', 'not_created', 'response_gone', "
            "'rate_limited', 'overloaded', 'provider_error', 'processing_failed')",
            name="ck_guided_campaign_attempt_error_category",
        ),
        sa.CheckConstraint(
            "request_sha256 IS NULL OR length(request_sha256) = 64",
            name="ck_guided_campaign_attempt_request_digest",
        ),
        sa.CheckConstraint(
            "state NOT IN ('dispatched', 'captured', 'processed') "
            "OR provider_response_id IS NOT NULL",
            name="ck_guided_campaign_attempt_dispatched_has_response",
        ),
        sa.CheckConstraint(
            "state NOT IN ('captured', 'processed') OR model_call_id IS NOT NULL",
            name="ck_guided_campaign_attempt_captured_has_model_call",
        ),
        sa.CheckConstraint(
            "state = 'planned' OR request_sha256 IS NOT NULL",
            name="ck_guided_campaign_attempt_dispatched_has_request",
        ),
        sa.CheckConstraint(
            "state NOT IN ('dispatched', 'captured', 'processed', 'uncertain') "
            "OR dispatched_at IS NOT NULL",
            name="ck_guided_campaign_attempt_dispatched_has_timestamp",
        ),
        sa.CheckConstraint(
            "state NOT IN ('captured', 'processed') OR request_envelope IS NULL",
            name="ck_guided_campaign_attempt_captured_drops_request",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["guided_campaigns.id"]),
        sa.ForeignKeyConstraint(["model_call_id"], ["model_calls.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "position", name="uq_guided_campaign_attempt_position"),
        sa.UniqueConstraint("provider_response_id", name="uq_guided_campaign_attempt_response_id"),
        sa.UniqueConstraint("model_call_id", name="uq_guided_campaign_attempt_model_call"),
    )
    op.create_table(
        "guided_campaign_branch_outcomes",
        sa.Column("branch_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("matched_queries", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('searched', 'truncated', 'unvisited', 'unattributable')",
            name="ck_guided_branch_outcome_outcome",
        ),
        sa.ForeignKeyConstraint(["branch_id"], ["guided_campaign_branches.id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["guided_campaign_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("branch_id", name="uq_guided_branch_outcome_branch"),
    )
    op.create_table(
        "guided_campaign_captured_urls",
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("url_sha256", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(length=40), nullable=False),
        sa.Column("model_selected", sa.Boolean(), nullable=False),
        sa.Column("model_why", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("staged_basis", sa.String(length=40), nullable=True),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("robots_basis", sa.String(length=40), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("length(url_sha256) = 64", name="ck_guided_captured_url_digest"),
        sa.CheckConstraint(
            "robots_basis IS NULL OR robots_basis IN ('allowed', 'single_posting_read')",
            name="ck_guided_captured_url_robots_basis",
        ),
        sa.CheckConstraint(
            "classification IN ('ats_detail', 'job_page_candidate', 'board', 'other')",
            name="ck_guided_captured_url_classification",
        ),
        sa.CheckConstraint(
            "outcome IN ('pending_read', 'staged', 'known_posting', 'registered_board', "
            "'board_admitted', 'screened_out', 'held_out', 'unreadable', 'policy_refused', "
            "'unresolved')",
            name="ck_guided_captured_url_outcome",
        ),
        sa.CheckConstraint(
            "staged_basis IS NULL OR staged_basis IN "
            "('title_evidence', 'body_focus_term', 'model_selected')",
            name="ck_guided_captured_url_staged_basis",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["guided_campaigns.id"]),
        sa.ForeignKeyConstraint(["pending_discovery_id"], ["pending_discoveries.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "url_sha256", name="uq_guided_captured_url"),
    )
    op.create_table(
        "guided_campaign_url_observations",
        sa.Column("captured_url_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("action_kind", sa.String(length=20), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "action_kind IN ('search_source', 'open_page', 'find_in_page', 'model_written')",
            name="ck_guided_url_observation_action_kind",
        ),
        sa.ForeignKeyConstraint(["captured_url_id"], ["guided_campaign_captured_urls.id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["guided_campaign_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_guided_url_observation_captured",
        "guided_campaign_url_observations",
        ["captured_url_id"],
        unique=False,
    )
    op.create_table(
        "guided_campaign_admitted_discoveries",
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("captured_url_id", sa.String(length=36), nullable=False),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["guided_campaign_attempts.id"]),
        sa.ForeignKeyConstraint(["captured_url_id"], ["guided_campaign_captured_urls.id"]),
        sa.ForeignKeyConstraint(["pending_discovery_id"], ["pending_discoveries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "captured_url_id", "pending_discovery_id", name="uq_guided_admitted_discovery"
        ),
    )
    op.create_index(
        "ix_guided_admitted_discovery_attempt",
        "guided_campaign_admitted_discoveries",
        ["attempt_id"],
        unique=False,
    )
    op.create_table(
        "relevance_reviews",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("master_cv_version_id", sa.String(length=36), nullable=False),
        sa.Column("search_parameter_set_id", sa.String(length=36), nullable=False),
        sa.Column("recommendation", sa.String(length=40), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("core_work_alignment", sa.String(length=40), nullable=True),
        sa.Column("core_qualification_alignment", sa.String(length=40), nullable=True),
        sa.Column("hard_condition", sa.String(length=40), nullable=True),
        sa.Column("decisive_basis", sa.String(length=40), nullable=True),
        sa.Column("posting_citations", sa.JSON(), nullable=False),
        sa.Column("master_cv_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("unresolved_questions", sa.JSON(), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.Column("source_url_sha256", sa.String(length=64), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("search_context_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", UTCDateTime(), nullable=False),
        sa.Column("instructions_digest", sa.String(length=64), nullable=True),
        sa.Column("schema_digest", sa.String(length=64), nullable=True),
        sa.Column("matched_exclusion", sa.Text(), nullable=True),
        sa.Column("keep_note_sha256", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_relevance_review_pending_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["background_tasks.workspace_id", "background_tasks.id"],
            name="fk_relevance_review_task_workspace",
        ),
        sa.ForeignKeyConstraint(["master_cv_version_id"], ["master_cv_versions.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "search_parameter_set_id"],
            ["job_search_parameter_sets.workspace_id", "job_search_parameter_sets.id"],
            name="fk_relevance_review_parameters_workspace",
        ),
        sa.CheckConstraint(
            "recommendation IN ('review', 'investigate', 'unlikely', 'insufficient_evidence')",
            name="ck_relevance_review_recommendation",
        ),
        sa.CheckConstraint(
            "core_work_alignment IN ('direct', 'transferable', 'absent', 'unresolved')",
            name="ck_relevance_review_core_work",
        ),
        sa.CheckConstraint(
            "core_qualification_alignment IN "
            "('established', 'partially_established', 'absent', 'unresolved')",
            name="ck_relevance_review_core_qualification",
        ),
        sa.CheckConstraint(
            "hard_condition IN ('passes', 'conflicts', 'unresolved')",
            name="ck_relevance_review_hard_condition",
        ),
        sa.CheckConstraint(
            "decisive_basis IN ('core_match', "
            "'transferable_match', 'core_gap', 'hard_conflict', 'logistics_unknown', "
            "'insufficient_posting')",
            name="ck_relevance_review_decisive_basis",
        ),
        sa.CheckConstraint(
            "(recommendation IS NULL AND core_work_alignment IS NULL "
            "AND core_qualification_alignment IS NULL AND hard_condition IS NULL "
            "AND decisive_basis IS NULL) OR (recommendation IS NOT NULL "
            "AND core_work_alignment IS NOT NULL AND core_qualification_alignment IS NOT NULL "
            "AND hard_condition IS NOT NULL AND decisive_basis IS NOT NULL)",
            name="ck_relevance_review_classes_together",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_relevance_review_latest",
        "relevance_reviews",
        ["pending_discovery_id", "reviewed_at"],
        unique=False,
    )
    op.create_table(
        "posting_observation_diffs",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("from_observation_id", sa.String(length=36), nullable=False),
        sa.Column("to_observation_id", sa.String(length=36), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("body_diff", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["from_observation_id"],
            ["posting_observations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_observation_id"],
            ["posting_observations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_observation_id", "to_observation_id", name="uq_observation_diff"),
    )
    op.create_index(
        "ix_observation_diff_job",
        "posting_observation_diffs",
        ["job_posting_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "cost_records",
        sa.Column("model_call_id", sa.String(length=36), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("currency", sa.String(length=12), nullable=False),
        sa.Column("pricing_source", sa.String(length=300), nullable=False),
        sa.Column("price_snapshot", sa.String(length=120), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["model_calls.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "evaluation_factors",
        sa.Column("evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("factor_type", sa.String(length=20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("factor_type IN ('pro', 'con', 'gap', 'risk')", name="ck_factor_type"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["job_evaluations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_id", "factor_type", "position", name="uq_factor_position"),
    )
    op.create_table(
        "evaluation_scores",
        sa.Column("evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("component", sa.String(length=80), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("weight", sa.Numeric(precision=7, scale=6), nullable=True),
        sa.Column("finding_state", sa.String(length=24), nullable=False),
        sa.Column("no_evidence_reason", sa.Text(), nullable=True),
        sa.Column("standing_source", sa.String(length=24), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "component IN ('fit', 'compensation', 'eligibility', 'evidence', 'opportunity')",
            name="ck_evaluation_score_component",
        ),
        sa.CheckConstraint(
            "(score IS NOT NULL AND finding_state = 'supported' AND no_evidence_reason IS NULL) OR (score IS NULL AND finding_state IN ('not_stated', 'conflict') AND no_evidence_reason IS NOT NULL)",
            name="ck_evaluation_score_finding",
        ),
        sa.CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 100", name="ck_evaluation_score_range"
        ),
        sa.CheckConstraint(
            "standing_source IS NULL OR standing_source IN ('compared', 'extracted')",
            name="ck_evaluation_score_standing_source",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["job_evaluations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_id", "component", name="uq_evaluation_score"),
    )
    op.create_table(
        "match_evidence",
        sa.Column("evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.Column("master_cv_entry_id", sa.String(length=160), nullable=True),
        sa.Column("master_cv_entry_key", sa.String(length=36), nullable=True),
        sa.Column("requirement_id", sa.String(length=160), nullable=True),
        sa.Column("requirement_key", sa.String(length=36), nullable=True),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=24), nullable=False),
        sa.Column("strength", sa.String(length=40), nullable=True),
        sa.Column("classification", sa.String(length=80), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "artifact_id IS NOT NULL AND ((char_start IS NOT NULL AND char_end IS NOT NULL) OR (length(trim(evidence_text)) > 0 AND length(trim(locator)) > 0))",
            name="ck_match_evidence_anchor",
        ),
        sa.CheckConstraint(
            "classification IN ('DIRECT', 'TRANSFERABLE', 'GAP', 'TAILORING_ONLY', 'ASSERTED')",
            name="ck_match_evidence_classification",
        ),
        sa.CheckConstraint(
            "(requirement_id IS NULL AND requirement_key IS NULL) OR (requirement_id IS NOT NULL AND requirement_key IS NOT NULL)",
            name="ck_match_evidence_requirement_identity",
        ),
        sa.CheckConstraint(
            "source_kind IN ('posting', 'candidate')", name="ck_match_evidence_source_kind"
        ),
        sa.CheckConstraint(
            "strength IS NULL OR strength IN ('strong', 'moderate', 'weak', 'none')",
            name="ck_match_evidence_strength",
        ),
        sa.CheckConstraint(
            "(char_start IS NULL AND char_end IS NULL) OR (char_start >= 0 AND char_end > char_start)",
            name="ck_match_evidence_span",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["job_evaluations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "posting_requirements",
        sa.Column("job_posting_id", sa.String(length=36), nullable=False),
        sa.Column("job_evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=64), nullable=False),
        sa.Column("requirement_key", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("importance", sa.String(length=40), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_hard_gate", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("gate_kind", sa.String(length=80), nullable=True),
        sa.Column("gate_quote", sa.Text(), nullable=True),
        sa.Column("gate_blocking", sa.Boolean(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "gate_kind IS NULL OR gate_kind IN ('location', 'onsite_cadence', 'travel', "
            "'work_authorization', 'sponsorship', 'time_zone', 'employment_type', "
            "'compensation', 'degree', 'minimum_years', 'credential', 'clearance', "
            "'leadership', 'other')",
            name="ck_posting_requirement_gate_kind",
        ),
        sa.CheckConstraint(
            "importance IN ('must_have', 'nice_to_have', 'responsibility')",
            name="ck_posting_requirement_importance",
        ),
        sa.ForeignKeyConstraint(
            ["job_evaluation_id"],
            ["job_evaluations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_evaluation_id", "requirement_id", name="uq_posting_requirement_identity"
        ),
        sa.UniqueConstraint(
            "job_evaluation_id", "requirement_key", name="uq_posting_requirement_key"
        ),
    )
    op.create_index(
        "ix_posting_requirement_posting_key",
        "posting_requirements",
        ["job_posting_id", "requirement_key"],
        unique=False,
    )
    op.create_index(
        "ix_posting_requirement_gate",
        "posting_requirements",
        ["job_evaluation_id", "is_hard_gate"],
        unique=False,
        sqlite_where=sa.text("is_hard_gate = 1"),
    )
    op.create_table(
        "evaluation_factor_evidence",
        sa.Column("evaluation_factor_id", sa.String(length=36), nullable=False),
        sa.Column("match_evidence_id", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "direction IN ('supports', 'contradicts', 'limits')",
            name="ck_factor_evidence_direction",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_factor_id"],
            ["evaluation_factors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["match_evidence_id"],
            ["match_evidence.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_factor_id", "match_evidence_id", name="uq_factor_evidence"),
    )
    op.create_table(
        "evaluation_score_evidence",
        sa.Column("evaluation_score_id", sa.String(length=36), nullable=False),
        sa.Column("match_evidence_id", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "direction IN ('supports', 'contradicts', 'limits')", name="ck_score_evidence_direction"
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_score_id"],
            ["evaluation_scores.id"],
        ),
        sa.ForeignKeyConstraint(
            ["match_evidence_id"],
            ["match_evidence.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_score_id", "match_evidence_id", name="uq_score_evidence"),
    )
    op.create_table(
        "requirement_rulings",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("requirement_key", sa.String(length=36), nullable=False),
        sa.Column("ruling", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "ruling IN ('met', 'not_applicable', 'disputed_fit', 'disputed_extraction', 'reversed')",
            name="ck_requirement_ruling_vocab",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "requirement_key", name="uq_workspace_requirement_ruling"
        ),
    )

    op.create_table(
        "relevance_review_disputes",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=False),
        sa.Column("relevance_review_id", sa.String(length=36), nullable=False),
        sa.Column("disposition", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "disposition IN ('set_aside')",
            name="ck_relevance_review_dispute_vocab",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_relevance_review_dispute_pending_workspace",
        ),
        sa.ForeignKeyConstraint(["relevance_review_id"], ["relevance_reviews.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "pending_discovery_id", name="uq_relevance_review_dispute_pending"
        ),
    )

    op.create_table(
        "relevance_review_rejections",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("pending_discovery_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("failure_code", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("rejected_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("origin", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "failure_code IN ('missing_result', 'duplicate_result', 'wrong_posting_span', "
            "'invalid_cv_evidence', 'inconsistent_verdict', 'leading_excerpt_only')",
            name="ck_relevance_review_rejection_vocab",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_relevance_review_rejection_pending_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["background_tasks.workspace_id", "background_tasks.id"],
            name="fk_relevance_review_rejection_task_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "pending_discovery_id", name="uq_relevance_review_rejection_pending"
        ),
    )

    _protect_immutable_tables()
    _create_chain_validator()
    _create_campaign_attempt_transition_validator()
    _seed_default_score_policy()


def downgrade() -> None:
    """Drop everything. There is no earlier schema to return to."""
    _drop_campaign_attempt_transition_validator()
    _drop_chain_validator()
    _remove_immutable_protection()
    op.drop_table("relevance_review_rejections")
    op.drop_table("relevance_review_disputes")
    op.drop_table("requirement_rulings")
    op.drop_table("evaluation_score_evidence")
    op.drop_table("evaluation_factor_evidence")
    op.drop_index("ix_posting_requirement_posting_key", table_name="posting_requirements")
    op.drop_table("posting_requirements")
    op.drop_table("match_evidence")
    op.drop_table("evaluation_scores")
    op.drop_table("evaluation_factors")
    op.drop_table("cost_records")
    op.drop_table("posting_observation_diffs")
    op.drop_index("ix_relevance_review_latest", table_name="relevance_reviews")
    op.drop_table("relevance_reviews")
    op.drop_index(
        "ix_guided_admitted_discovery_attempt", table_name="guided_campaign_admitted_discoveries"
    )
    op.drop_table("guided_campaign_admitted_discoveries")
    op.drop_index(
        "ix_guided_url_observation_captured", table_name="guided_campaign_url_observations"
    )
    op.drop_table("guided_campaign_url_observations")
    op.drop_table("guided_campaign_captured_urls")
    op.drop_table("guided_campaign_branch_outcomes")
    op.drop_table("guided_campaign_attempts")
    op.drop_table("guided_campaign_branches")
    op.drop_table("guided_campaign_states")
    op.drop_table("guided_campaigns")
    op.drop_table("response_cache_archive")
    op.drop_table("model_call_envelopes")
    op.drop_table("model_calls")
    op.drop_index(
        "ix_evaluation_currentness_review_evaluation",
        table_name="evaluation_currentness_reviews",
    )
    op.drop_table("evaluation_currentness_reviews")
    op.drop_table("job_evaluations")
    op.drop_table("eligibility_records")
    op.drop_table("compensation_components")
    op.drop_table("batch_members")
    op.drop_table("run_prompt_versions")
    op.drop_table("posting_observations")
    op.drop_table("batch_runs")
    op.drop_table("approval_decisions")
    op.drop_table("review_proposals")
    op.drop_index("ix_pending_content_history", table_name="pending_posting_contents")
    op.drop_table("pending_posting_contents")
    op.drop_table("artifacts")
    op.drop_table("application_events")
    op.drop_table("tailoring_runs")
    op.drop_table("master_cv_proposals")
    op.drop_table("refusal_pattern_proposals")
    op.drop_table("posting_refusals")
    op.drop_index("ix_screened_out_title_last_seen", table_name="screened_out_titles")
    op.drop_table("screened_out_titles")
    op.drop_index("ix_source_scan_outcome_history", table_name="source_scan_outcomes")
    op.drop_index("ix_source_scan_outcome_source_scan", table_name="source_scan_outcomes")
    op.drop_index("ix_source_scan_outcome_workspace_url", table_name="source_scan_outcomes")
    op.drop_index("ix_source_scan_outcome_workspace_reason", table_name="source_scan_outcomes")
    op.drop_index("ix_source_scan_outcome_workspace_task", table_name="source_scan_outcomes")
    op.drop_table("source_scan_outcomes")
    op.drop_index("ix_source_frontier_listing_membership", table_name="source_scan_frontier_items")
    op.drop_index("ix_source_frontier_work", table_name="source_scan_frontier_items")
    op.drop_table("source_scan_frontier_items")
    op.drop_table("source_listing_memberships")
    op.drop_index("uq_source_scan_member_reserved_source", table_name="source_scan_members")
    op.drop_table("source_scan_members")
    op.drop_table("source_scan_continuations")
    op.drop_table("source_scans")
    op.drop_table("posting_urls")
    op.drop_table("posting_identifiers")
    op.drop_table("applications")
    op.drop_table("task_events")
    op.drop_table("tag_assignments")
    op.drop_table("score_weight_components")
    op.drop_table("prompt_versions")
    op.drop_index(
        "ix_missed_requirement_workspace_cv",
        table_name="missed_requirement_texts_v1",
    )
    op.drop_table("missed_requirement_texts_v1")
    op.drop_table("derived_clusters")
    op.drop_table("derived_employer_archetypes")
    op.drop_table("derived_search_titles")
    op.drop_index("ix_cv_entry_key", table_name="master_cv_entries")
    op.drop_index("ix_cv_entry_version_position", table_name="master_cv_entries")
    op.drop_table("master_cv_entries")
    op.drop_table("master_cv_versions")
    op.drop_table("job_postings")
    op.drop_table("tags")
    op.drop_table("sources")
    op.drop_table("score_weight_sets")
    op.drop_table("saved_views")
    op.drop_table("prompt_templates")
    op.drop_index(
        "ix_pending_disposition_latest", table_name="pending_discovery_disposition_events"
    )
    op.drop_table("pending_discovery_disposition_events")
    op.drop_table("pending_discoveries")
    op.drop_table("notes")
    op.drop_table("learning_loop_policy_versions")
    op.drop_table("guardrail_versions")
    op.drop_table("job_search_parameter_sets")
    op.drop_table("employers")
    op.drop_table("candidate_profiles")
    op.drop_table("worker_presence")
    op.drop_index("ix_task_workspace_type_correlation", table_name="background_tasks")
    op.drop_index("ix_task_type_started", table_name="background_tasks")
    op.drop_index("ix_task_workspace_type_status", table_name="background_tasks")
    op.drop_table("background_tasks")
    op.drop_table("audit_events")
    op.drop_table("workspaces")
